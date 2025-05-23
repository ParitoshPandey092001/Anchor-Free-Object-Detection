import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torchvision.transforms as transforms
import torchvision.models as models
import torch.backends.cudnn as cudnn
from torch.nn import init as init

import warnings
warnings.filterwarnings('ignore')

import os,sys,time
from .nn_utils import *
from termcolor import cprint

#from utils.core import print_info

class M2Det(nn.Module):                                 # Multi-level Multi-scale single-shot object Detector
    def __init__(self, phase, size, config = None):
        super(M2Det,self).__init__()
        self.phase = phase
        self.size = size
        self.init_params(config)
        #print_info('===> Constructing M2Det model', ['yellow','bold'])
        self.construct_modules()

    def init_params(self, config=None): # Directly read the config
        assert config is not None, 'Error: no config'  
        for key,value in config.items():
            #if check_argu(key,value):
            setattr(self, key, value)

    def construct_modules(self,):
        # construct tums
        for i in range(self.num_levels):
            if i == 0:
                setattr(self,
                        'unet{}'.format(i+1),
                        TUM(first_level = True, 
                            input_planes = self.planes//2, 
                            is_smooth = self.smooth,
                            scales = self.num_scales,
                            side_channel = 512)) #side channel isn't fixed.
            else:
                setattr(self,
                        'unet{}'.format(i + 1),
                        TUM(first_level = False, 
                            input_planes = self.planes//2, 
                            is_smooth = self.smooth, 
                            scales = self.num_scales,
                            side_channel = self.planes))
        
        # construct base features
        if 'vgg' in self.net_family:
            #self.base = nn.ModuleList(get_backbone(self.backbone))
            shallow_in, shallow_out = 512,256
            deep_in, deep_out = 1024,512
       
        elif 'res' in self.net_family: # Including ResNet series and ResNeXt series
            #self.base = get_backbone(self.backbone)
            shallow_in, shallow_out = 512,256
            deep_in, deep_out = 1024,256
        
        self.reduce= BasicConv(shallow_in, shallow_out, 
                               kernel_size = 3, 
                               stride = 1, 
                               padding = 1
                               )
        self.up_reduce= BasicConv(deep_in, deep_out, 
                                  kernel_size = 1, 
                                  stride = 1
                                  )
        
        # construct others
        if self.phase == 'test':
            self.softmax = nn.Softmax().cuda()
        self.Norm = nn.BatchNorm2d(256).cuda()#I changed from 256*8
        self.leach = nn.ModuleList([BasicConv(
                    deep_out+shallow_out,
                    self.planes//2,
                    kernel_size = (1,1),stride=(1,1))]*self.num_levels).cuda()
        '''
        # construct localization and recognition layers
        loc_ = list()
        conf_ = list()
        for i in range(self.num_scales):
            loc_.append(nn.Conv2d(self.planes*self.num_levels,
                                       4 * 6, # 4 is coordinates, 6 is anchors for each pixels,
                                       3, 1, 1))
            conf_.append(nn.Conv2d(self.planes*self.num_levels,
                                       self.num_classes * 6, #6 is anchors for each pixels,
                                       3, 1, 1))
        self.loc = nn.ModuleList(loc_)
        self.conf = nn.ModuleList(conf_)
        '''
        
        # construct SFAM module
        #if self.sfam:
        self.sfam_module = SFAM(self.planes, self.num_levels, self.num_scales, 
                                compress_ratio = 16
                                )
    
    def forward(self,x1,x2):
        base_feats = [x1,x2]                   
        self.reduce = self.reduce.cuda()
        self.up_reduce = self.up_reduce.cuda()
       
        base_feature = torch.cat(
                (self.reduce(base_feats[0]), 
                 F.interpolate(self.up_reduce(base_feats[1]),
                               scale_factor = 2,
                               mode = 'nearest'
                               )),
                               1
                )
        base_feature = base_feature
        #print('crossed')
        #print("base feature shape:",base_feature.shape)
        # tum_outs is the multi-level multi-scale feature
        
        tum_outs = [getattr(self, 'unet{}'.format(1))(self.leach[0](base_feature), 'none')]
        #for i in tum_outs:
            #print("tum_out shape:",i[0].shape)

        for i in range(1,self.num_levels,1):
            tum_outs.append(
                    getattr(self, 'unet{}'.format(i+1))(
                        self.leach[i](base_feature), tum_outs[i-1][-1]
                        )
                    )
        # concat with same scales
        sources = [torch.cat([_fx[i-1] for _fx in tum_outs],1) for i in range(self.num_scales, 0, -1)]
        
        # forward_sfam
        if self.sfam:
            sources = self.sfam_module(sources)
        sources[0] = self.Norm(sources[0]).cuda()
        return sources
    
    def init_model(self, base_model_path):
        if self.backbone == 'vgg16':
            if isinstance(base_model_path, str):
                base_weights = torch.load(base_model_path)
                print_info('Loading base network...')
                self.base.load_state_dict(base_weights)
        elif 'res' in self.backbone:
            pass # pretrained seresnet models are initially loaded when defining them.
        
        def weights_init(m):
            for key in m.state_dict():
                if key.split('.')[-1] == 'weight':
                    if 'conv' in key:
                        init.kaiming_normal_(m.state_dict()[key], mode='fan_out')
                    if 'bn' in key:
                        m.state_dict()[key][...] = 1
                elif key.split('.')[-1] == 'bias':
                    m.state_dict()[key][...] = 0
        
        print_info('Initializing weights for [tums, reduce, up_reduce, leach, loc, conf]...')
        for i in range(self.num_levels):
            getattr(self,'unet{}'.format(i + 1)).apply(weights_init)
        self.reduce.apply(weights_init)
        self.up_reduce.apply(weights_init)
        self.leach.apply(weights_init)
        self.loc.apply(weights_init)
        self.conf.apply(weights_init)

    def load_weights(self, base_file):
        other, ext = os.path.splitext(base_file)
        if ext == '.pkl' or '.pth':
            print_info('Loading weights into state dict...')
            self.load_state_dict(torch.load(base_file))
            print_info('Finished!')
        
        else:
            print_info('Sorry only .pth and .pkl files supported.')

    

def build_net(phase = 'train', size = 320, config = None):
    
    return M2Det(phase, size, config)

def print_info(info, _type = None):
        if _type is not None:
            if isinstance(info,str):
                cprint(info, _type[0], attrs = [_type[1]])
            
            elif isinstance(info,list):
                for i in range(info):
                    cprint(i, _type[0], attrs = [_type[1]])
        
        else:
            print(info)
