import argparse
import copy
import time

import networkx as nx 
import sklearn.metrics as metrics
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader

from torch_geometric.datasets import Planetoid
from torch_geometric.datasets import TUDataset
import torch_geometric.transforms as T
import torch_geometric.nn as pyg_nn

from utils import *
from deepsnap.hetero_graph import HeteroGraph
from deepsnap.dataset import GraphDataset
from deepsnap.batch import Batch
from deepsnap.hetero_gnn import *


def arg_parse():
    parser = argparse.ArgumentParser(description='Link pred arguments.')
    parser.add_argument('--device', type=str,
                        help='CPU / GPU device.')
    parser.add_argument('--data_path', type=str,
                        help='Path to wordnet nx gpickle file.')
    parser.add_argument('--epochs', type=int,
                        help='Number of epochs to train.')
    parser.add_argument('--mode', type=str,
                        help='Link prediction mode. Disjoint or all.')
    parser.add_argument('--model', type=str,
                        help='MlpMessage.')
    parser.add_argument('--edge_message_ratio', type=float,
                        help='Ratio of edges used for message-passing (only in disjoint mode).')
    parser.add_argument('--neg_sampling_ratio', type=float,
                        help='Ratio of the number of negative examples to the number of positive examples')
    parser.add_argument('--hidden_dim', type=int,
                        help='Hidden dimension of GNN.')

    parser.set_defaults(
            device='cuda:0', 
            data_path='../data/WN18.gpickle',
            epochs=500,
            mode='disjoint',
            model='MlpMessage',
            edge_message_ratio=0.8,
            neg_sampling_ratio=1.0,
            hidden_dim=16,
    )
    return parser.parse_args()

def WN_transform(G, num_edge_types, input_dim=5):
    H = nx.MultiDiGraph()
    for node in G.nodes():
        H.add_node(node, node_type='n1', node_feature=torch.ones(input_dim))
    for u, v, edge_key in G.edges:
        l = G[u][v][edge_key]['e_label']
        e_feat = torch.zeros(num_edge_types)
        e_feat[l] = 1.
        H.add_edge(u, v, edge_feature=e_feat, edge_type=str(l.item()))
    return H

class HeteroNet(torch.nn.Module):
    def __init__(self, hete, hidden_size, dropout):
        super(HeteroNet, self).__init__()
        
        conv1, conv2 = generate_convs(hete, HeteroSAGEConv, hidden_size, task='link_pred')
        self.conv1 = HeteroConv(conv1)
        self.conv2 = HeteroConv(conv2)
        self.loss_fn = torch.nn.BCEWithLogitsLoss()
        self.dropout = dropout

    def forward(self, data):
        x = forward_op(data.node_feature, F.dropout, p=self.dropout, training=self.training)
        x = forward_op(x, F.relu)
        x = self.conv1(x, data.edge_index)
        x = forward_op(x, F.dropout, p=self.dropout, training=self.training)
        x = forward_op(x, F.relu)
        x = self.conv2(x, data.edge_index)

        pred = {}
        for message_type in data.edge_label_index:
            nodes_first = torch.index_select(x['n1'], 0, data.edge_label_index[message_type][0,:].long())
            nodes_second = torch.index_select(x['n1'], 0, data.edge_label_index[message_type][1,:].long())
            pred[message_type] = torch.sum(nodes_first * nodes_second, dim=-1)
        return pred

    def loss(self, pred, y, edge_label_index):
        loss = 0
        for key in pred:
            p = torch.sigmoid(pred[key])
            loss += self.loss_fn(p, y[key].type(pred[key].dtype))
        return loss

def train(model, dataloaders, optimizer, args):
    val_max = 0
    best_model = model
    t_accu = []
    v_accu = []
    e_accu = []
    for epoch in range(1, args.epochs):
        for iter_i, batch in enumerate(dataloaders['train']):
            batch.to(args.device)
            model.train()
            optimizer.zero_grad()
            pred = model(batch)
            loss = model.loss(pred, batch.edge_label, batch.edge_label_index)
            loss.backward()
            optimizer.step()

            log = 'Epoch: {:03d}, Train loss: {:.4f}, Train: {:.4f}, Val: {:.4f}, Test: {:.4f}'
            accs = test(model, dataloaders, args)
            t_accu.append(accs['train'])
            v_accu.append(accs['val'])
            e_accu.append(accs['test'])

            print(log.format(epoch, loss.item(), accs['train'], accs['val'], accs['test']))
            if val_max < accs['val']:
                val_max = accs['val']
                best_model = copy.deepcopy(model)

    log = 'Best, Train: {:.4f}, Val: {:.4f}, Test: {:.4f}'
    accs = test(best_model, dataloaders, args)
    print(log.format(accs['train'], accs['val'], accs['test']))

def test(model, dataloaders, args):
    model.eval()
    accs = {}
    for mode, dataloader in dataloaders.items():
        acc = 0
        for i, batch in enumerate(dataloader):
            num = 0
            batch.to(args.device)
            pred = model(batch)
            for key in pred:
                p = torch.sigmoid(pred[key]).cpu().detach().numpy()
                pred_label = np.zeros_like(p, dtype=np.int64)
                pred_label[np.where(p > 0.5)[0]] = 1
                pred_label[np.where(p <= 0.5)[0]] = 0
                acc += np.sum(pred_label == batch.edge_label[key].cpu().numpy())
                num += len(pred_label)
        accs[mode] = acc / num
    return accs

def main():
    args = arg_parse()

    edge_train_mode = args.mode
    print('edge train mode: {}'.format(edge_train_mode))

    G = nx.read_gpickle(args.data_path)
    print(G.number_of_edges())
    print('Each node has node ID (n_id). Example: ', G.nodes[0])
    print('Each edge has edge ID (id) and categorical label (e_label). Example: ', G[0][5871])

    # find num edge types
    max_label = 0
    labels = []
    for u, v, edge_key in G.edges:
        l = G[u][v][edge_key]['e_label']
        if not l in labels:
            labels.append(l)
    # labels are consecutive (0-17)
    num_edge_types = len(labels)

    H = WN_transform(G, num_edge_types)
    hete = HeteroGraph(H)

    dataset = GraphDataset([hete], task='link_pred')
    dataset_train, dataset_val, dataset_test = dataset.split(transductive=True,
                                                            split_ratio=[0.8, 0.1, 0.1])
    train_loader = DataLoader(dataset_train, collate_fn=Batch.collate(),
                        batch_size=1)
    val_loader = DataLoader(dataset_val, collate_fn=Batch.collate(),
                        batch_size=1)
    test_loader = DataLoader(dataset_test, collate_fn=Batch.collate(),
                        batch_size=1)
    dataloaders = {'train': train_loader, 'val': val_loader, 'test': test_loader}

    hidden_size = 32
    model = HeteroNet(hete, hidden_size, 0.2).to(args.device)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001, weight_decay=5e-4)

    train(model, dataloaders, optimizer, args)

if __name__ == '__main__':
    main()
