import torch, random, scipy, math
import torch.nn as nn
import torch.nn.functional as F
import pandas as pd
import numpy as np
import re
from atom3d.datasets import LMDBDataset
from .neighbors import get_subunits, get_negatives
from torch.utils.data import IterableDataset
from . import GVP, GVPConvLayer, LayerNorm, TransformerConv
import torch_cluster, torch_geometric, torch_scatter
from .data import _normalize, _rbf

import atom3d.protein.sequence as seq

# ignorre warnings
import warnings
warnings.filterwarnings("ignore")

_NUM_ATOM_TYPES = 9
_element_mapping = lambda x: {
    'H' : 0,
    'C' : 1,
    'N' : 2,
    'O' : 3,
    'F' : 4,
    'S' : 5,
    'Cl': 6, 'CL': 6,
    'P' : 7
}.get(x, 8)
_amino_acids = lambda x: {
    'ALA': 0,
    'ARG': 1,
    'ASN': 2,
    'ASP': 3,
    'CYS': 4,
    'GLU': 5,
    'GLN': 6,
    'GLY': 7,
    'HIS': 8,
    'ILE': 9,
    'LEU': 10,
    'LYS': 11,
    'MET': 12,
    'PHE': 13,
    'PRO': 14,
    'SER': 15,
    'THR': 16,
    'TRP': 17,
    'TYR': 18,
    'VAL': 19
}.get(x, 20)

_reverse__amino_acids = lambda x: {
 0: 'ALA',
 1: 'ARG',
 2: 'ASN',
 3: 'ASP',
 4: 'CYS',
 5: 'GLU',
 6: 'GLN',
 7: 'GLY',
 8: 'HIS',
 9: 'ILE',
 10: 'LEU',
 11: 'LYS',
 12: 'MET',
 13: 'PHE',
 14: 'PRO',
 15: 'SER',
 16: 'THR',
 17: 'TRP',
 18: 'TYR',
 19: 'VAL'}.get(x, 'UNKOWN')

map_amino_3to1 = lambda x: {
    'ALA': 'A',
    'ARG': 'R',
    'ASN': 'N',
    'ASP': 'D',
    'CYS': 'C',
    'GLN': 'Q',
    'GLU': 'E',
    'GLY': 'G',
    'HIS': 'H',
    'ILE': 'I',
    'LEU': 'L',
    'LYS': 'K',
    'MET': 'M',
    'PHE': 'F',
    'PRO': 'P',
    'SER': 'S',
    'THR': 'T',
    'TRP': 'W',
    'TYR': 'Y',
    'VAL': 'V',
}.get(x, 'unkown')

map_amino_1to3 = lambda x: {
    'A': 'ALA',
    'R': 'ARG',
    'N': 'ASN',
    'D': 'ASP',
    'C': 'CYS',
    'Q': 'GLN',
    'E': 'GLU',
    'G': 'GLY',
    'H': 'HIS',
    'I': 'ILE',
    'L': 'LEU',
    'K': 'LYS',
    'M': 'MET',
    'F': 'PHE',
    'P': 'PRO',
    'S': 'SER',
    'T': 'THR',
    'W': 'TRP',
    'Y': 'TYR',
    'V': 'VAL',
}.get(x, 'unknown')




_DEFAULT_V_DIM = (100, 16)
_DEFAULT_E_DIM = (32, 1)

def _edge_features(coords, edge_index, D_max=4.5, num_rbf=16, device='cpu'):
    
    E_vectors = coords[edge_index[0]] - coords[edge_index[1]]
    rbf = _rbf(E_vectors.norm(dim=-1), 
               D_max=D_max, D_count=num_rbf, device=device)

    edge_s = rbf
    edge_v = _normalize(E_vectors).unsqueeze(-2)

    edge_s, edge_v = map(torch.nan_to_num,
            (edge_s, edge_v))

    return edge_s, edge_v

class BaseTransform:
    '''
    Implementation of an ATOM3D Transform which featurizes the atomic
    coordinates in an ATOM3D dataframes into `torch_geometric.data.Data`
    graphs. This class should not be used directly; instead, use the
    task-specific transforms, which all extend BaseTransform. Node
    and edge features are as described in the EGNN manuscript.
    
    Returned graphs have the following attributes:
    -x          atomic coordinates, shape [n_nodes, 3]
    -atoms      numeric encoding of atomic identity, shape [n_nodes]
    -edge_index edge indices, shape [2, n_edges]
    -edge_s     edge scalar features, shape [n_edges, 16]
    -edge_v     edge scalar features, shape [n_edges, 1, 3]
    
    Subclasses of BaseTransform will produce graphs with additional 
    attributes for the tasks-specific training labels, in addition 
    to the above.
    
    All subclasses of BaseTransform directly inherit the BaseTransform
    constructor.
    
    :param edge_cutoff: distance cutoff to use when drawing edges
    :param num_rbf: number of radial bases to encode the distance on each edge
    :device: if "cuda", will do preprocessing on the GPU
    '''
    def __init__(self, edge_cutoff=4.5, num_rbf=16, device='cpu'):
        self.edge_cutoff = edge_cutoff
        self.num_rbf = num_rbf
        self.device = device
            
    def __call__(self, df):
        '''
        :param df: `pandas.DataFrame` of atomic coordinates
                    in the ATOM3D format
        
        :return: `torch_geometric.data.Data` structure graph
        '''
        with torch.no_grad():
            coords = torch.as_tensor(df[['x', 'y', 'z']].to_numpy(),
                                     dtype=torch.float32, device=self.device)
            atoms = torch.as_tensor(list(map(_element_mapping, df.element)),
                                            dtype=torch.long, device=self.device)

            edge_index = torch_cluster.radius_graph(coords, r=self.edge_cutoff)

            edge_s, edge_v = _edge_features(coords, edge_index, 
                                D_max=self.edge_cutoff, num_rbf=self.num_rbf, device=self.device)

            return torch_geometric.data.Data(x=coords, atoms=atoms,
                        edge_index=edge_index, edge_s=edge_s, edge_v=edge_v)

class BaseModel(nn.Module):
    '''
    A base 5-layer GVP-GNN for all ATOM3D tasks, using GVPs with 
    vector gating as described in the manuscript. Takes in atomic-level
    structure graphs of type `torch_geometric.data.Batch`
    and returns a single scalar.
    
    This class should not be used directly. Instead, please use the
    task-specific models which extend BaseModel. (Some of these classes
    may be aliases of BaseModel.)
    
    :param num_rbf: number of radial bases to use in the edge embedding
    '''
    def __init__(self, num_rbf=16, use_transformer=False, use_bert_embedding=False, use_bert_predict=False):
        
        super().__init__()
        activations = (F.relu, None)
        self.embed = nn.Embedding(_NUM_ATOM_TYPES, _NUM_ATOM_TYPES)
        
        self.W_e = nn.Sequential(
            LayerNorm((num_rbf, 1)),
            GVP((num_rbf, 1), _DEFAULT_E_DIM, 
                activations=(None, None), vector_gate=True)
        )
        
        self.W_v = nn.Sequential(
            LayerNorm((_NUM_ATOM_TYPES, 0)),
            GVP((_NUM_ATOM_TYPES, 0), _DEFAULT_V_DIM,
                activations=(None, None), vector_gate=True)
        )
                
        self.layers = nn.ModuleList(
                GVPConvLayer(_DEFAULT_V_DIM, _DEFAULT_E_DIM, 
                             activations=activations, vector_gate=True, use_transformer=use_transformer) 
            for _ in range(5) )
        
        ns, _ = _DEFAULT_V_DIM
        self.W_out = nn.Sequential(
            LayerNorm(_DEFAULT_V_DIM),
            GVP(_DEFAULT_V_DIM, (ns, 0), 
                activations=activations, vector_gate=True)
        )
        
        self.dense = nn.Sequential(
            nn.Linear(ns, 2*ns), nn.ReLU(inplace=True),
            nn.Dropout(p=0.1),
            nn.Linear(2*ns, 1)
        )
    
    def forward(self, batch, scatter_mean=True, dense=True):
        '''
        Forward pass which can be adjusted based on task formulation.
        
        :param batch: `torch_geometric.data.Batch` with data attributes
                      as returned from a BaseTransform
        :param scatter_mean: if `True`, returns mean of final node embeddings
                             (for each graph), else, returns embeddings seperately
        :param dense: if `True`, applies final dense layer to reduce embedding
                      to a single scalar; else, returns the embedding
        '''
        # print("batch.chain_sequence", len(batch.chain_sequence[0]), len(batch.atoms))

        # batch.chain_sequence)
        h_V = self.embed(batch.atoms)
        h_E = (batch.edge_s, batch.edge_v)
        h_V = self.W_v(h_V)
        h_E = self.W_e(h_E)
        
        batch_id = batch.batch
        
        for layer in self.layers:
            h_V = layer(h_V, batch.edge_index, h_E)

        out = self.W_out(h_V)
        if scatter_mean: out = torch_scatter.scatter_mean(out, batch_id, dim=0)
        if dense: out = self.dense(out).squeeze(-1)
        return out

########################################################################

class SMPTransform(BaseTransform):
    '''
    Transforms dict-style entries from the ATOM3D SMP dataset
    to featurized graphs. Returns a `torch_geometric.data.Data`
    graph with attribute `label` and all structural attributes
    as described in BaseTransform.
    
    Includes hydrogen atoms.
    '''
    def __call__(self, elem):
        data = super().__call__(elem['atoms'])
        with torch.no_grad():
            data.label = torch.as_tensor(elem['labels'], 
                            device=self.device, dtype=torch.float32)
        return data
        
SMPModel = BaseModel
    
########################################################################

class PPIDataset(IterableDataset):
    '''
    A `torch.utils.data.IterableDataset` wrapper around a
    ATOM3D PPI dataset. Extracts (many) individual amino acid pairs
    from each structure of two interacting proteins. The returned graphs
    are seperate and each represents a 30 angstrom radius from the 
    selected residue's alpha carbon.
    
    On each iteration, returns a pair of `torch_geometric.data.Data`
    graphs with the (same) attribute `label` which is 1 if the two
    amino acids interact and 0 otherwise, `ca_idx` for the node index
    of the alpha carbon, and all structural attributes as 
    described in BaseTransform.
    
    Modified from
    https://github.com/drorlab/atom3d/blob/master/examples/ppi/gnn/data.py
    
    Excludes hydrogen atoms.
    
    :param lmdb_dataset: path to ATOM3D dataset
    '''
    def __init__(self, lmdb_dataset):
        self.dataset = LMDBDataset(lmdb_dataset)
        self.transform = BaseTransform()
        
    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            gen = self._dataset_generator(list(range(len(self.dataset))), shuffle=True)
        else:  
            per_worker = int(math.ceil(len(self.dataset) / float(worker_info.num_workers)))
            worker_id = worker_info.id
            iter_start = worker_id * per_worker
            iter_end = min(iter_start + per_worker, len(self.dataset))
            gen = self._dataset_generator(
                list(range(len(self.dataset)))[iter_start:iter_end],
                shuffle=True)
        return gen

    def _df_to_graph(self, struct_df, chain_res, label):
        
        struct_df = struct_df[struct_df.element != 'H'].reset_index(drop=True)

        chain, resnum = chain_res
        res_df = struct_df[(struct_df.chain == chain) & (struct_df.residue == resnum)]
        if 'CA' not in res_df.name.tolist():
            return None
        ca_pos = res_df[res_df['name']=='CA'][['x', 'y', 'z']].astype(np.float32).to_numpy()[0]

        kd_tree = scipy.spatial.KDTree(struct_df[['x','y','z']].to_numpy())
        graph_pt_idx = kd_tree.query_ball_point(ca_pos, r=30.0, p=2.0)
        graph_df = struct_df.iloc[graph_pt_idx].reset_index(drop=True)
        
        ca_idx = np.where((graph_df.chain == chain) & (graph_df.residue == resnum) & (graph_df.name == 'CA'))[0]
        if len(ca_idx) != 1:
            return None
        
        data = self.transform(graph_df)
        data.label = label
        
        data.ca_idx = int(ca_idx)
        data.n_nodes = data.num_nodes

        return data

    def _dataset_generator(self, indices, shuffle=True):
        if shuffle: random.shuffle(indices)
        with torch.no_grad():
            for idx in indices:
                data = self.dataset[idx]

                neighbors = data['atoms_neighbors']
                pairs = data['atoms_pairs']
                
                for i, (ensemble_name, target_df) in enumerate(pairs.groupby(['ensemble'])):
                    sub_names, (bound1, bound2, _, _) = nb.get_subunits(target_df)
                    positives = neighbors[neighbors.ensemble0 == ensemble_name]
                    negatives = nb.get_negatives(positives, bound1, bound2)
                    negatives['label'] = 0
                    labels = self._create_labels(positives, negatives, num_pos=10, neg_pos_ratio=1)
                    
                    for index, row in labels.iterrows():
                    
                        label = float(row['label'])
                        chain_res1 = row[['chain0', 'residue0']].values
                        chain_res2 = row[['chain1', 'residue1']].values
                        graph1 = self._df_to_graph(bound1, chain_res1, label)
                        graph2 = self._df_to_graph(bound2, chain_res2, label)
                        if (graph1 is None) or (graph2 is None):
                            continue
                        yield graph1, graph2

    def _create_labels(self, positives, negatives, num_pos, neg_pos_ratio):
        frac = min(1, num_pos / positives.shape[0])
        positives = positives.sample(frac=frac)
        n = positives.shape[0] * neg_pos_ratio
        n = min(negatives.shape[0], n)
        negatives = negatives.sample(n, random_state=0, axis=0)
        labels = pd.concat([positives, negatives])[['chain0', 'residue0', 'chain1', 'residue1', 'label']]
        return labels

class PPIModel(BaseModel):
    '''
    GVP-GNN for the PPI task.
    
    Extends BaseModel to accept a tuple (batch1, batch2)
    of `torch_geometric.data.Batch` graphs, where each graph
    index in a batch is paired with the same graph index in the
    other batch.
    
    As noted in the manuscript, PPIModel uses the final alpha
    carbon embeddings instead of the graph mean embedding.
    
    Returns a single scalar for each graph pair which can be used as
    a logit in binary classification.
    '''
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        ns, _ = _DEFAULT_V_DIM
        self.dense = nn.Sequential(
            nn.Linear(2*ns, 4*ns), nn.ReLU(inplace=True),
            nn.Dropout(p=0.1),
            nn.Linear(4*ns, 1)
        )

    def forward(self, batch):   
        graph1, graph2 = batch
        out1, out2 = map(self._gnn_forward, (graph1, graph2))
        out = torch.cat([out1, out2], dim=-1)
        out = self.dense(out)
        return torch.sigmoid(out).squeeze(-1)
    
    def _gnn_forward(self, graph):
        out = super().forward(graph, scatter_mean=False, dense=False)
        return out[graph.ca_idx+graph.ptr[:-1]]


########################################################################

class LBATransform(BaseTransform):
    '''
    Transforms dict-style entries from the ATOM3D LBA dataset
    to featurized graphs. Returns a `torch_geometric.data.Data`
    graph with attribute `label` for the neglog-affinity
    and all structural attributes as described in BaseTransform.
    
    The transform combines the atomic coordinates of the pocket
    and ligand atoms and treats them as a single structure / graph. 
    
    Includes hydrogen atoms.
    '''
    def __call__(self, elem):
        pocket, ligand = elem['atoms_pocket'], elem['atoms_ligand']
        df = pd.concat([pocket, ligand], ignore_index=True)
        
        data = super().__call__(df)
        with torch.no_grad():
            data.label = elem['scores']['neglog_aff']
            lig_flag = torch.zeros(df.shape[0], device=self.device, dtype=torch.bool)
            lig_flag[-len(ligand):] = 1
            data.lig_flag = lig_flag
        return data

LBAModel = BaseModel
    
########################################################################
    
class LEPTransform(BaseTransform):
    '''
    Transforms dict-style entries from the ATOM3D LEP dataset
    to featurized graphs. Returns a tuple (active, inactive) of 
    `torch_geometric.data.Data` graphs with the (same) attribute
    `label` which is equal to 1. if the ligand activates the protein
    and 0. otherwise, and all structural attributes as described
    in BaseTransform.
    
    The transform combines the atomic coordinates of the pocket
    and ligand atoms and treats them as a single structure / graph.
    
    Excludes hydrogen atoms.
    '''
    def __call__(self, elem):
        active, inactive = elem['atoms_active'], elem['atoms_inactive']
        with torch.no_grad():
            active, inactive = map(self._to_graph, (active, inactive))
        active.label = inactive.label = 1. if elem['label'] == 'A' else 0.
        return active, inactive
        
    def _to_graph(self, df):
        df = df[df.element != 'H'].reset_index(drop=True)
        return super().__call__(df)                        

class LEPModel(BaseModel):
    '''
    GVP-GNN for the LEP task.
    
    Extends BaseModel to accept a tuple (batch1, batch2)
    of `torch_geometric.data.Batch` graphs, where each graph
    index in a batch is paired with the same graph index in the
    other batch.
    
    Returns a single scalar for each graph pair which can be used as
    a logit in binary classification.
    '''
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        ns, _ = _DEFAULT_V_DIM
        self.dense = nn.Sequential(
            nn.Linear(2*ns, 4*ns), nn.ReLU(inplace=True),
            nn.Dropout(p=0.1),
            nn.Linear(4*ns, 1)
        )
        
    def forward(self, batch):        
        out1, out2 = map(self._gnn_forward, batch)
        out = torch.cat([out1, out2], dim=-1)
        out = self.dense(out)
        return torch.sigmoid(out).squeeze(-1)
    
    def _gnn_forward(self, graph):
        return super().forward(graph, dense=False)
    
########################################################################

class MSPTransform(BaseTransform):    
    '''
    Transforms dict-style entries from the ATOM3D MSP dataset
    to featurized graphs. Returns a tuple (original, mutated) of 
    `torch_geometric.data.Data` graphs with the (same) attribute
    `label` which is equal to 1. if the mutation stabilizes the 
    complex and 0. otherwise, and all structural attributes as 
    described in BaseTransform.
    
    The transform combines the atomic coordinates of the two proteis
    in each complex and treats them as a single structure / graph.
    
    Adapted from
    https://github.com/drorlab/atom3d/blob/master/examples/msp/gnn/data.py
    
    Excludes hydrogen atoms.
    '''
    def __call__(self, elem):
        mutation = elem['id'].split('_')[-1]
        orig_df = elem['original_atoms'].reset_index(drop=True)
        mut_df = elem['mutated_atoms'].reset_index(drop=True)
        with torch.no_grad():
            original, mutated = self._transform(orig_df, mutation), \
                                self._transform(mut_df, mutation)
        original.label = mutated.label = 1. if elem['label'] == '1' else 0.
        return original, mutated
    
    def _transform(self, df, mutation):
        
        df = df[df.element != 'H'].reset_index(drop=True)
        data = super().__call__(df)
        data.node_mask = self._extract_node_mask(df, mutation)
        return data
    
    def _extract_node_mask(self, df, mutation):
        chain, res = mutation[1], int(mutation[2:-1])
        idx = df.index[(df.chain.values == chain) & (df.residue.values == res)].values
        mask = torch.zeros(len(df), dtype=torch.long, device=self.device)
        mask[idx] = 1
        return mask
                                
class MSPModel(BaseModel):
    '''
    GVP-GNN for the MSP task.
    
    Extends BaseModel to accept a tuple (batch1, batch2)
    of `torch_geometric.data.Batch` graphs, where each graph
    index in a batch is paired with the same graph index in the
    other batch.
    
    As noted in the manuscript, MSPModel uses the final embeddings
    averaged over the residue of interest instead of the entire graph.
    
    Returns a single scalar for each graph pair which can be used as
    a logit in binary classification.
    '''
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        ns, _ = _DEFAULT_V_DIM
        self.dense = nn.Sequential(
            nn.Linear(2*ns, 4*ns), nn.ReLU(inplace=True),
            nn.Dropout(p=0.1),
            nn.Linear(4*ns, 1)
        )
        
    def forward(self, batch):        
        out1, out2 = map(self._gnn_forward, batch)
        out = torch.cat([out1, out2], dim=-1)
        out = self.dense(out)
        return torch.sigmoid(out).squeeze(-1)
    
    def _gnn_forward(self, graph):
        out = super().forward(graph, scatter_mean=False, dense=False)
        out = out * graph.node_mask.unsqueeze(-1)
        out = torch_scatter.scatter_add(out, graph.batch, dim=0)
        count = torch_scatter.scatter_add(graph.node_mask, graph.batch)
        return out / count.unsqueeze(-1)
        
########################################################################
        
class PSRTransform(BaseTransform):     
    '''
    Transforms dict-style entries from the ATOM3D PSR dataset
    to featurized graphs. Returns a `torch_geometric.data.Data`
    graph with attribute `label` for the GDT_TS, `id` for the
    name of the target, and all structural attributes as 
    described in BaseTransform.
    
    Includes hydrogen atoms.
    '''
    def __call__(self, elem):
        df = elem['atoms']
        df = df[df.element != 'H'].reset_index(drop=True)
        data = super().__call__(df)
        data.label = elem['scores']['gdt_ts']
        data.id = eval(elem['id'])[0]
        return data

PSRModel = BaseModel

########################################################################
        
class RSRTransform(BaseTransform):     
    '''
    Transforms dict-style entries from the ATOM3D RSR dataset
    to featurized graphs. Returns a `torch_geometric.data.Data`
    graph with attribute `label` for the RMSD, `id` for the
    name of the target, and all structural attributes as 
    described in BaseTransform.
    
    Includes hydrogen atoms.
    '''
    def __call__(self, elem):
        df = elem['atoms']
        df = df[df.element != 'H'].reset_index(drop=True)
        data = super().__call__(df)
        data.label = elem['scores']['rms']
        data.id = eval(elem['id'])[0]
        return data

RSRModel = BaseModel

########################################################################

class RESTransform(BaseTransform):
    '''
    Transforms dict-style entries from the ATOM3D RES dataset to add
    a 'chain_sequences' attribute to the graph, which is a list of
    the amino acid sequences of each chain in the protein.
    '''
    # from Bio.PDB.Polypeptide import protein_letters_3to1

    def __call__(self, df):
        data = super().__call__(df)
        with torch.no_grad():
            chain_sequences = seq.get_chain_sequences(df)
            chain_sequence = chain_sequences[0][-1]
            sequence_length = len(chain_sequence)
            chain_sequence = " ".join(list(re.sub(r"[UZOB]", "X", chain_sequence)))
            data.update({'chain_sequence': chain_sequence, 'sequence_length': sequence_length})
        return data
    
class RESDataset(IterableDataset):
    '''
    A `torch.utils.data.IterableDataset` wrapper around a
    ATOM3D RES dataset.
    
    On each iteration, returns a `torch_geometric.data.Data`
    graph with the attribute `label` encoding the masked residue
    identity, `ca_idx` for the node index of the alpha carbon, 
    and all structural attributes as described in BaseTransform.
    
    Excludes hydrogen atoms.
    
    :param lmdb_dataset: path to ATOM3D dataset
    :param split_path: path to the ATOM3D split file
    '''
    def __init__(self, lmdb_dataset, split_path):
        self.dataset = LMDBDataset(lmdb_dataset)
        self.idx = list(map(int, open(split_path).read().split()))
        self.transform = RESTransform()
        
    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            gen = self._dataset_generator(list(range(len(self.idx))), 
                      shuffle=True)
        else:  
            per_worker = int(math.ceil(len(self.idx) / float(worker_info.num_workers)))
            worker_id = worker_info.id
            iter_start = worker_id * per_worker
            iter_end = min(iter_start + per_worker, len(self.idx))
            gen = self._dataset_generator(list(range(len(self.idx)))[iter_start:iter_end],
                      shuffle=True)
        return gen
    
    def _dataset_generator(self, indices, shuffle=True):
        if shuffle: random.shuffle(indices)
        with torch.no_grad():
            for idx in indices:
                data = self.dataset[self.idx[idx]]
                atoms = data['atoms']
                for sub in data['labels'].itertuples():
                    _, num, aa = sub.subunit.split('_')
                    num, aa = int(num), _amino_acids(aa)
                    if aa == 20: continue
                    my_atoms = atoms.iloc[data['subunit_indices'][sub.Index]].reset_index(drop=True)
                    ca_idx = np.where((my_atoms.residue == num) & (my_atoms.name == 'CA'))[0]
                    if len(ca_idx) != 1: continue
                    with torch.no_grad():
                        graph = self.transform(my_atoms)
                        graph.label = aa
                        graph.ca_idx = int(ca_idx)
                        yield graph
                        
class RESModel(BaseModel):
    '''
    GVP-GNN for the RES task.
    
    Extends BaseModel to output a 20-dim vector instead of a single
    scalar for each graph, which can be used as logits in 20-way
    classification.
    
    As noted in the manuscript, RESModel uses the final alpha
    carbon embeddings instead of the graph mean embedding.
    '''
    def __init__(self, **kwargs):
        super().__init__(**kwargs) 
        _SEQ_EMBED_SIZE = 1024       
        AMINO_TYPE = 20
        AMINO_TYPE_AND_MASK = 21
     
        
        self.bert = False
        if kwargs['use_bert_predict']:
            
            # Define MLP architecture to combine GVP and BERT predictions
            self.mlp = nn.Sequential(
                    nn.Linear(40, 64),  # Input size: 40, output size: 64
                    nn.ReLU(),          # Apply ReLU activation function
                    nn.Linear(64, 20)   # Input size: 64, output size: 20
                )
            
            self.bert = True

            model_name = "prot_bert"
            # model_name = "prot_t5_xl_half_uniref50-enc"

            if model_name == "prot_bert":
                # from transformers import BertModel, BertTokenizer, pipeline
                from transformers import BertForMaskedLM, BertTokenizer, pipeline
                tokenizer = BertTokenizer.from_pretrained("Rostlab/prot_bert", do_lower_case=False )
                model = BertForMaskedLM.from_pretrained("Rostlab/prot_bert")
            
            # TO FIX -----------------------------------
            # if model_name == "prot_t5_xl_half_uniref50-enc":
            #     from transformers import T5EncoderModel, T5Tokenizer, pipeline
            #     # tokenizer = T5Tokenizer.from_pretrained("Rostlab/prot_t5_xl_uniref50", do_lower_case=False)
            #     # model = T5EncoderModel.from_pretrained("Rostlab/prot_t5_xl_uniref50")
            #     tokenizer = T5Tokenizer.from_pretrained("Rostlab/prot_t5_xl_half_uniref50-enc", do_lower_case=False)
            #     model = T5EncoderModel.from_pretrained("Rostlab/prot_t5_xl_half_uniref50-enc")

            # self.device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
            # self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
            self.unmasker = pipeline('fill-mask', model=model, tokenizer=tokenizer,  
                                    #  device=self.device,  
                                     top_k=20)

   
        
        if kwargs['use_bert_embedding']:  

            # load bert amino embeddings
            model_name = "prot_bert"  # or 'prot_t5_xl_half_uniref50-enc'     
            # model_name = "prot_t5_xl_half_uniref50-enc"  # or 'prot_t5_xl_half_uniref50-enc'     
            weights = torch.load(f"data/AMINO_TYPES_andMask_EMB_{model_name}.pt")
            print(f"loading bert amino embeddings from {model_name}")
        
            self.embed_aminos = nn.Embedding.from_pretrained(torch.tensor(weights), freeze=False) # [22,1024]  20 Amino + 1 Mask + 1 Padding
            self.embed_atom = nn.Embedding(_NUM_ATOM_TYPES, _SEQ_EMBED_SIZE)

            # combine bert amino embeddings and atom embeddings
            combined = torch.cat((self.embed_aminos.weight , self.embed_atom.weight), dim=0)
            self.embed = nn.Embedding.from_pretrained(torch.tensor(combined))               # [22,1024]  20 Amino + 1 Mask + 1 Padding
            
            self.W_v = nn.Sequential(
            LayerNorm((_SEQ_EMBED_SIZE, 0)),
            GVP((_SEQ_EMBED_SIZE, 0), _DEFAULT_V_DIM,
                activations=(None, None), vector_gate=True)
            )
        else:
            self.embed = nn.Embedding(_NUM_ATOM_TYPES, _NUM_ATOM_TYPES)
        
        ns, _ = _DEFAULT_V_DIM
        self.dense = nn.Sequential(
            nn.Linear(ns, 2*ns), nn.ReLU(inplace=True),
            nn.Dropout(p=0.1),
            nn.Linear(2*ns, 20)
        )

    def forward(self, batch):
        out = super().forward(batch, scatter_mean=False)
                
        if self.bert:

            out_tmp = _reverse__amino_acids(int(batch.label))
            tmp =  map_amino_3to1(out_tmp)

            # Split each letter into separate indices
            split_list = list(batch.chain_sequence[0])
            # Insert 'MASK' at the specified index
            split_list.insert(batch.ca_idx, ' [MASK]')
            # Join the list back into a single string
            output_string = ''.join(split_list)
            # Create the output list with the desired format
            output_list = [output_string]
            bert_prediction = self.unmasker(output_list)  # --> output is a dict with the 
            
            softmax_GVP = out[batch.ca_idx+batch.ptr[:-1]]
            
            only_bert = torch.zeros_like(softmax_GVP, requires_grad=False)
            softmax_bert = only_bert.clone()

            for item in bert_prediction:
                a = str(map_amino_1to3(item['token_str']))
                idx = _amino_acids(a)
                if idx < 20:
                    confidence_score = item['score']
                    softmax_bert[:,idx] = confidence_score
            
            combined = torch.cat((softmax_bert, softmax_GVP), dim=1)

            combined = combined

            # Pass the concatenated tensor through the MLP
            output = self.mlp(combined)

            # combined softmax [1,20]
            return output
        
        else:   
            return out[batch.ca_idx+batch.ptr[:-1]]
    
