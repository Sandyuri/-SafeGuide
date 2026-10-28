"""Deterministic runtime device selection."""
import os

def select_device(torch):
    requested=os.environ.get('LSG_DEVICE','auto').strip().lower()
    if requested not in {'auto','cuda','cpu'}: raise ValueError('invalid_LSG_DEVICE')
    if requested=='cuda' and not torch.cuda.is_available(): raise RuntimeError('cuda_requested_but_unavailable')
    return 'cuda' if requested=='cuda' or requested=='auto' and torch.cuda.is_available() else 'cpu'
