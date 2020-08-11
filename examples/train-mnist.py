import os
import sys
import argparse
import logging
import time
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
import torchvision.datasets as datasets
import torchvision.transforms as transforms
#import petsc4py
#from petsc4py import PETSc

import copy
torch.manual_seed(0)
torch.backends.cudnn.deterministic = True
import inspect
from pytorch_model_summary import summary

#from gpu_mem_track import MemTracker
parser = argparse.ArgumentParser()
parser.add_argument('--network', type=str, choices=['resnet', 'odenet'], default='odenet')
parser.add_argument('--tol', type=float, default=1e-3)
parser.add_argument('--adjoint', type=eval, default=False, choices=[True, False])
parser.add_argument('--downsampling-method', type=str, default='conv', choices=['conv', 'res'])
parser.add_argument('--nepochs', type=int, default=80)
parser.add_argument('--data_aug', type=eval, default=False, choices=[True, False])
parser.add_argument('--lr', type=float, default=0.1)
parser.add_argument('--batch_size', type=int, default=128)
parser.add_argument('--test_batch_size', type=int, default=1000)
parser.add_argument('--method',type = str, default = 'Euler', choices = ['Euler','RK2','RK4','Dopri5','Dopri5_fixed'])
parser.add_argument('--save', type=str, default='./experiment1')
parser.add_argument('--debug', action='store_true')
parser.add_argument('--breakpoint', type=eval, default=False, choices=[True, False])
parser.add_argument('--gpu', type=int, default=1)
parser.add_argument('--Nt',type=int, default = 1)
parser.add_argument('--impl',type=str, default='ANODE', choices = ['NODE','ANODE','NODE_adj','PETSc'])
parser.add_argument('--implicit', action='store_true')
parser.add_argument('--double_prec', action='store_true')
args, unknown = parser.parse_known_args()
sys.argv = [sys.argv[0]] + unknown
import petsc4py
petsc4py.init(sys.argv)
from petsc4py import PETSc
sys.path.append('/home/zhaow/torchdiffeq')
sys.path.append('/home/zhaow/anode')
torch.cuda.set_device(1)
device = torch.device('cuda:' + str(args.gpu) if torch.cuda.is_available() else 'cpu')
tensor_type = torch.float32
if args.double_prec:
    tensor_type = torch.float64

import torchdiffeq
if args.implicit:
    from torchdiffeq.petscutil import petsc_adjoint_implicit as petsc_adjoint
else:
    from torchdiffeq.petscutil import petsc_adjoint_explicit_cuda as petsc_adjoint


if args.impl == 'NODE_adj':
    from torchdiffeq import odeint_adjoint as odeint
else:
    from torchdiffeq import odeint

from anode import odesolver_adjoint as odesolver


def conv3x3(in_planes, out_planes, stride=1):
    """3x3 convolution with padding"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride, padding=1, bias=False)


def conv1x1(in_planes, out_planes, stride=1):
    """1x1 convolution"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=1, stride=stride, bias=False)


def norm(dim):
    return nn.GroupNorm(min(32, dim), dim)


class ResBlock(nn.Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super(ResBlock, self).__init__()
        self.norm1 = norm(inplanes)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.conv1 = conv3x3(inplanes, planes, stride)
        self.norm2 = norm(planes)
        self.conv2 = conv3x3(planes, planes)

    def forward(self, x):
        shortcut = x

        out = self.relu(self.norm1(x))

        if self.downsample is not None:
            shortcut = self.downsample(out)

        out = self.conv1(out)
        out = self.norm2(out)
        out = self.relu(out)
        out = self.conv2(out)

        return out + shortcut


class ConcatConv2d(nn.Module):

    def __init__(self, dim_in, dim_out, ksize=3, stride=1, padding=0, dilation=1, groups=1, bias=True, transpose=False):
        super(ConcatConv2d, self).__init__()
        module = nn.ConvTranspose2d if transpose else nn.Conv2d
        self._layer = module(
            dim_in + 1, dim_out, kernel_size=ksize, stride=stride, padding=padding, dilation=dilation, groups=groups,
            bias=bias
        )

    def forward(self, t, x):
        tt = torch.ones_like(x[:, :1, :, :]) * t
        ttx = torch.cat([tt, x], 1)
        return self._layer(ttx)


class ODEfunc(nn.Module):

    def __init__(self, dim):
        super(ODEfunc, self).__init__()
        self.norm1 = norm(dim)
        self.relu = nn.ReLU(inplace=True)
        self.conv1 = ConcatConv2d(dim, dim, 3, 1, 1)
        self.norm2 = norm(dim)
        self.conv2 = ConcatConv2d(dim, dim, 3, 1, 1)
        self.norm3 = norm(dim)
        self.nfe = 0

    def forward(self, t, x):
        self.nfe += 1
        out = self.norm1(x)
        out = self.relu(out)
        out = self.conv1(t, out)
        out = self.norm2(out)
        out = self.relu(out)
        out = self.conv2(t, out)
        out = self.norm3(out)
        return out


class ODEBlock_NODE(nn.Module):

    def __init__(self, odefunc):
        super(ODEBlock_NODE, self).__init__()
        if args.double_prec:
            self.odefunc = odefunc.double().to(device)
        else:
            self.odefunc = odefunc.to(device)
        
  #      if args.method == 'Dopri5':
        self.integration_time = torch.tensor(  [0,1] ).float()
        #else:
        #    self.integration_time = torch.tensor(  [1./(float(args.Nt))*i for i in range(args.Nt+1)] ).float()
        self.options = {}
        if not args.method == 'Dopri5':
            self.options.update({'step_size' : 1./float(args.Nt)})
    

    def forward(self, x):
        
        if args.method == 'Euler':
            Method = 'euler'
            
        elif args.method == 'RK2':
            Method = 'midpoint'
        elif args.method == 'RK4':
            Method = 'rk4'
        elif args.method == 'Dopri5':
            Method = 'dopri5'

        elif args.method == 'Dopri5_fixed':
            Method = 'dopri5_fixed'
          
        self.integration_time = self.integration_time.type_as(x)
        out = odeint(self.odefunc, x.to(tensor_type), self.integration_time, rtol=args.tol, atol=args.tol,method = Method, options=self.options)
           
        return out[-1].to(torch.float32)

    @property
    def nfe(self):
        return self.odefunc.nfe

    @nfe.setter
    def nfe(self, value):
        self.odefunc.nfe = value

class ODEBlock_ANODE(nn.Module):

    def __init__(self, odefunc):
        super(ODEBlock_ANODE, self).__init__()
        if args.double_prec:
            self.odefunc = odefunc.double().to(device)
        else:
            self.odefunc = odefunc.to(device)
        self.options = {}
        self.options.update({'Nt':args.Nt})
        #self.options.update({'method':'RK4_alt'})
        if args.method == 'Euler':
            self.options.update({'method':'Euler'})
        elif args.method == 'RK2':
            self.options.update({'method':'RK2'})
        elif args.method == 'RK4':
            #self.options.update({'method':'RK4_alt'})
            self.options.update({'method':'RK4'})
            
        elif args.method == 'Dopri5_fixed':
            self.options.update({'method':'Dopri5'})
        
        print(self.options)

    def forward(self, x):
        
        out = odesolver(self.odefunc, x.to(tensor_type), self.options)
        return out.to(torch.float32)

    @property
    def nfe(self):
        return self.odefunc.nfe

    @nfe.setter
    def nfe(self, value):
        self.odefunc.nfe = value

class ODEBlock_PETSc(nn.Module):

    def __init__(self, odefunc, train=True):
        super(ODEBlock_PETSc, self).__init__()
        if args.double_prec:
            self.odefunc = odefunc.double().to(device)
        else:
            self.odefunc = odefunc.to(device)
        
        self.options = {}
        self.ode = petsc_adjoint.ODEPetsc()
        self.step_size = 1./float(args.Nt)
        if args.method == 'Euler':
            self.method = 'euler'
            
        elif args.method == 'RK2':
            self.method = 'midpoint'
        elif args.method == 'RK4':
            self.method = 'rk4'
        elif args.method == 'Dopri5':
            self.method = 'dopri5'

        elif args.method == 'Dopri5_fixed':
            self.method = 'dopri5_fixed'
        
        self.integration_time = torch.tensor(  [0,1] ).float()
        self.train = train
        
        if self.train:
            self.ode.setupTS(torch.zeros(args.batch_size,64,6,6).to(device,tensor_type), self.odefunc, self.step_size, self.method, enable_adjoint=True)
        else:
            self.ode.setupTS(torch.zeros(args.test_batch_size,64,6,6).to(device,tensor_type), self.odefunc, self.step_size, self.method, enable_adjoint=False)
       

    def forward(self, x):

        out = self.ode.odeint_adjoint(x.to(tensor_type), self.integration_time.type_as(x))
        return out[-1].to(torch.float32)

    @property
    def nfe(self):
        return self.odefunc.nfe

    @nfe.setter
    def nfe(self, value):
        self.odefunc.nfe = value 


class Flatten(nn.Module):

    def __init__(self):
        super(Flatten, self).__init__()

    def forward(self, x):
        shape = torch.prod(torch.tensor(x.shape[1:])).item()
        return x.view(-1, shape)


class RunningAverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self, momentum=0.99):
        self.momentum = momentum
        self.reset()

    def reset(self):
        self.val = None
        self.avg = 0

    def update(self, val):
        if self.val is None:
            self.avg = val
        else:
            self.avg = self.avg * self.momentum + val * (1 - self.momentum)
        self.val = val


def get_mnist_loaders(data_aug=False, batch_size=128, test_batch_size=1000, perc=1.0):
    
    if data_aug:
        transform_train = transforms.Compose([
            transforms.RandomCrop(28, padding=4),
            transforms.ToTensor(),
        ])
    else:
        transform_train = transforms.Compose([
            transforms.ToTensor(),
        ])

    transform_test = transforms.Compose([
        transforms.ToTensor(),
    ])

    train_loader = DataLoader(
        datasets.MNIST(root='.data/mnist', train=True, download=True, transform=transform_train), batch_size=batch_size,
        shuffle=True, num_workers=2, drop_last=True
    )

    train_eval_loader = DataLoader(
        datasets.MNIST(root='.data/mnist', train=True, download=True, transform=transform_test),
        batch_size=test_batch_size, shuffle=False, num_workers=2, drop_last=True
    )

    test_loader = DataLoader(
        datasets.MNIST(root='.data/mnist', train=False, download=True, transform=transform_test),
        batch_size=test_batch_size, shuffle=False, num_workers=2, drop_last=True
    )
    return train_loader, test_loader, train_eval_loader


def inf_generator(iterable):
    """Allows training with DataLoaders in a single infinite loop:
        for i, (x, y) in enumerate(inf_generator(train_loader)):
    """
    iterator = iterable.__iter__()
    while True:
        try:
            yield iterator.__next__()
        except StopIteration:
            iterator = iterable.__iter__()


def learning_rate_with_decay(batch_size, batch_denom, batches_per_epoch, boundary_epochs, decay_rates):
    initial_learning_rate = args.lr * batch_size / batch_denom

    boundaries = [int(batches_per_epoch * epoch) for epoch in boundary_epochs]
    vals = [initial_learning_rate * decay for decay in decay_rates]

    def learning_rate_fn(itr):
        lt = [itr < b for b in boundaries] + [True]
        i = np.argmax(lt)
        return vals[i]

    return learning_rate_fn


def one_hot(x, K):
    return np.array(x[:, None] == np.arange(K)[None, :], dtype=int)


def accuracy(model, dataset_loader):
    
    total_correct = 0
    for x, y in dataset_loader:
        x = x.to(device)
        y = one_hot(np.array(y.numpy()), 10)

        target_class = np.argmax(y, axis=1)
        predicted_class = np.argmax(model(x).cpu().detach().numpy(), axis=1)
        total_correct += np.sum(predicted_class == target_class)
    
    return total_correct / len(dataset_loader.dataset)


def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)



def makedirs(dirname):
    if not os.path.exists(dirname):
        os.makedirs(dirname)


def get_logger(logpath, filepath, package_files=[], displaying=True, saving=True, debug=False):
    logger = logging.getLogger()
    if debug:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logger.setLevel(level)
    if saving:
        info_file_handler = logging.FileHandler(logpath, mode="a")
        info_file_handler.setLevel(level)
        logger.addHandler(info_file_handler)
    if displaying:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        logger.addHandler(console_handler)
    logger.info(filepath)
    with open(filepath, "r") as f:
        logger.info(f.read())

    for f in package_files:
        logger.info(f)
        with open(f, "r") as package_f:
            logger.info(package_f.read())

    return logger


if __name__ == '__main__':

    makedirs(args.save)
    logger = get_logger(logpath=os.path.join(args.save, 'logs'), filepath=os.path.abspath(__file__))
    logger.info(args)

    #device = torch.device('cuda:' + str(args.gpu) if torch.cuda.is_available() else 'cpu')

    is_odenet = args.network == 'odenet'

    if args.downsampling_method == 'conv':
        downsampling_layers = [
            nn.Conv2d(1, 64, 3, 1),
            norm(64),
            nn.ReLU(inplace=True),
            nn.Conv2d(64, 64, 4, 2, 1),
            
            norm(64),
            nn.ReLU(inplace=True),
            nn.Conv2d(64 , 64, 4, 2, 1),
        ]
        
    elif args.downsampling_method == 'res':
        downsampling_layers = [
            nn.Conv2d(1, 64, 3, 1),
            ResBlock(64, 64, stride=2, downsample=conv1x1(64, 64, 2)),
            ResBlock(64, 64, stride=2, downsample=conv1x1(64, 64, 2)),
        ]
        
    ODEfunc = ODEfunc(64)
    
    if args.impl == 'ANODE':
        ODEBlock = ODEBlock_ANODE
    elif args.impl == 'PETSc':
        ODEBlock = ODEBlock_PETSc
    else:

        ODEBlock = ODEBlock_NODE
    feature_layers = []
    if args.network == 'odenet':        
        feature_layers = [ODEBlock(ODEfunc)] 
        if args.impl == 'PETSc':
            feature_layers_test = [ODEBlock(ODEfunc, train=False)]
        else:
            feature_layers_test = feature_layers
    fc_layers = [norm(64), nn.ReLU(inplace=True), nn.AdaptiveAvgPool2d((1, 1)), Flatten(), nn.Linear(64, 10)]
    

    model = nn.Sequential(*downsampling_layers, *feature_layers, *fc_layers).to(device)
    #model_test = model
    model_test = nn.Sequential(*downsampling_layers, *feature_layers_test, *fc_layers).to(device)
    
    logger.info(model)
    logger.info('Number of parameters: {}'.format(count_parameters(model)))
    
   
    print(count_parameters(nn.Sequential(*feature_layers)))
    print(count_parameters(nn.Sequential(*fc_layers)))
    print(count_parameters(nn.Sequential(*downsampling_layers)))
    #import pdb; pdb.set_trace()

    criterion = nn.CrossEntropyLoss().to(device,tensor_type)

    
    
    train_loader, test_loader, train_eval_loader = get_mnist_loaders(
        args.data_aug, args.batch_size, args.test_batch_size
    )
    
    data_gen = inf_generator(train_loader)
    batches_per_epoch = len(train_loader)

    
    
    lr_fn = learning_rate_with_decay(
        args.batch_size, batch_denom=128, batches_per_epoch=batches_per_epoch, boundary_epochs=[60, 100, 140],
        decay_rates=[1, 0.1, 0.01, 0.001]
    )

    optimizer = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=0.9)
    
    
    
    best_acc = 0

   # batch_time_meter = RunningAverageMeter()
    f_nfe_meter = RunningAverageMeter()
    b_nfe_meter = RunningAverageMeter()
    
 
    train_loss = 0
    start = time.time()
    end = time.time()
    for itr in range(args.nepochs * batches_per_epoch):
        optimizer.zero_grad()
        torch.cuda.empty_cache()
        x, y = data_gen.__next__()
        x = x.to(device)
        y = y.to(device)
        
       
        
########################   NODE ###########################
       
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr_fn(itr)
            
        
        loss = criterion(model(x), y)
        
        if is_odenet:
            nfe_forward = feature_layers[0].nfe
            
            feature_layers[0].nfe = 0
        else:
            nfe_forward = 0
            
        loss.backward()
        
        optimizer.step()
        torch.cuda.empty_cache()
        if itr % batches_per_epoch == 1:
            train_loss = 0
        train_loss += loss.item()
        
        
        if is_odenet:
            nfe_backward = feature_layers[0].nfe
            feature_layers[0].nfe = 0
        else:
            nfe_backward = 0
           
        f_nfe_meter.update(nfe_forward)
        b_nfe_meter.update(nfe_backward)
        
      ############################################################################################
        if itr % batches_per_epoch == 0:
            with torch.no_grad():
                train_acc = accuracy(model_test, train_eval_loader)
                val_acc = accuracy(model_test, test_loader)
                
                
            if val_acc > best_acc:
                torch.save({'state_dict': model.state_dict(), 'args': args}, os.path.join(args.save, 'model.pth'))
                best_acc = val_acc
                    
              
                
            end = time.time()    
            logger.info( " Epoch {:04d} | Time per epoch {:.3f} | NFE-F {:.1f} | NFE-B {:.1f} | "
                     "Train Acc {:.4f} | Test Acc {:.4f} | Train Loss {:.10f}".format(
                            itr // batches_per_epoch, end-start, f_nfe_meter.avg,
                            b_nfe_meter.avg, train_acc, val_acc, train_loss 
                        )
                    )
            start = time.time()
        