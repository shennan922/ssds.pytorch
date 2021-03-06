import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

import os

from lib.layers import *

class SSDLite(nn.Module):
    """Single Shot Multibox Architecture for embeded system
    See: https://arxiv.org/pdf/1512.02325.pdf & 
    https://arxiv.org/pdf/1801.04381.pdf for more details.

    Args:
        phase: (string) Can be "eval" or "train" or "feature"
        base: base layers for input
        extras: extra layers that feed to multibox loc and conf layers
        head: "multibox head" consists of loc and conf conv layers
        feature_layer: the feature layers for head to loc and conf
        num_classes: num of classes 
    """

    def __init__(self, base, extras, head, feature_layer,
                 num_classes, conf_dist):
        super(SSDLite, self).__init__()
        self.onnx_export=False
        self.num_classes = num_classes
        # SSD network
        self.base = nn.ModuleList(base)
        #self.norm = L2Norm(feature_layer[1][0], 20)
        self.extras = nn.ModuleList(extras)

        self.loc = nn.ModuleList(head[0])
        self.conf = nn.ModuleList(head[1])
        self.softmax = nn.Softmax(dim=-1) if conf_dist=='softmax' else nn.Sigmoid()
        #self.softmax = nn.Sigmoid() if conf_dist == 'sigmoid' else nn.Softmax(dim=-1)

        #self.batch_norm = nn.BatchNorm2d(feature_layer[1][0]+feature_layer[1][1])
        self.feature_layer = feature_layer[0]

        # self.cfg_anchor_steps=None
        # self.cfg_anchor_sizes=None
        # self.cfg_aspect_ratios=None


    # def set_anchor_setting(self,cfg_anchor_steps=None,
    #                        cfg_anchor_sizes=None,
    #                        cfg_anchor_aspects=None):
    #     self.cfg_anchor_steps = cfg_anchor_steps
    #     self.cfg_anchor_sizes = cfg_anchor_sizes
    #     self.cfg_anchor_aspects= cfg_anchor_aspects


    def forward(self, x, phase='eval'):
        """Applies network layers and ops on input image(s) x.

        Args:
            x: input image or batch of images. Shape: [batch,3,300,300].

        Return:
            Depending on phase:
            test:
                Variable(tensor) of output class label predictions,
                confidence score, and corresponding location predictions for
                each object detected. Shape: [batch,topk,7]

            train:
                list of concat outputs from:
                    1: confidence layers, Shape: [batch*num_priors,num_classes]
                    2: localization layers, Shape: [batch,num_priors*4]

            feature:
                the features maps of the feature extractor
        """
        sources = list()
        loc = list()
        conf = list()
        #if self.onnx_export:
            #nchw
        #    x=x.permute(0,3,1,2)
        # apply bases layers and cache source layer outputs
        for k in range(len(self.base)):
            x = self.base[k](x)
            if k in self.feature_layer:
                #if len(sources) == 0:
                #    s = self.norm(x)
                #    sources.append(s)
                #else:
                sources.append(x)

        # source_1=F.upsample(sources[1],scale_factor=2)
        # source_0=torch.cat([sources[0],source_1],1)
        # source_0=self.batch_norm(source_0)
        # sources[0]=source_0

        # apply extra layers and cache source layer outputs
        for k, v in enumerate(self.extras):
            x = F.relu(v(x), inplace=True)
            sources.append(x)
            # if k % 2 == 1:
            #     sources.append(x)

        if phase == 'feature':
            return sources

        # apply multibox head to source layers
        for (x, l, c) in zip(sources, self.loc, self.conf):
            loc.append(l(x).permute(0, 2, 3, 1).contiguous())
            conf.append(c(x).permute(0, 2, 3, 1).contiguous())

        if self.onnx_export:
            loc = torch.cat([o.view(1,-1) for o in loc], 1)
            conf = torch.cat([o.view(1, -1) for o in conf], 1)
            #loc = torch.cat([o.view(-1,4) for o in loc], 0)
            #conf = torch.cat([o.view(-1, self.num_classes) for o in conf], 0)
        else:
            loc = torch.cat([o.view(o.size(0), -1) for o in loc], 1)
            conf = torch.cat([o.view(o.size(0), -1) for o in conf], 1)


        if self.onnx_export:
            #scores = self.softmax(conf)  # conf predsreturn loc,
            #return loc, scores
            output = (
                #3d tensor  batch * num_prior * 4
                loc.view(1, -1, 4),                   # loc preds
                #2d tensor (batch*num_prior) * 4
                self.softmax(conf.view(1, -1, self.num_classes)),  # conf preds
            )
        elif phase == 'eval':

            output = (
                #3d tensor  batch * num_prior * 4
                loc.view(loc.size(0), -1, 4),                   # loc preds
                #2d tensor (batch*num_prior) * 4
                self.softmax(conf.view(-1, self.num_classes)),  # conf preds
            )
        else:
            output = (
                loc.view(loc.size(0), -1, 4),
                conf.view(conf.size(0), -1, self.num_classes),
            )
        return output

def add_extras(base, feature_layer, mbox, num_classes):
    extra_layers = []
    loc_layers = []
    conf_layers = []
    in_channels = None

    for idx, (layer, depth, box) in enumerate(zip(feature_layer[0], feature_layer[1], mbox)):
        if layer == 'S':
            extra_layers += [ _conv_dw(in_channels, depth, stride=2, padding=1, expand_ratio=1) ]
            in_channels = depth
        elif layer == '':
            extra_layers += [ _conv_dw(in_channels, depth, stride=1, expand_ratio=1) ]
            in_channels = depth
        else:
            in_channels = depth
        #loc_layers += [nn.Conv2d(in_channels, box * 4, kernel_size=3, padding=1)]
        #conf_layers += [nn.Conv2d(in_channels, box * num_classes, kernel_size=3, padding=1)]
        #if idx == 0:
        #    in_channels = feature_layer[1][0]+feature_layer[1][1]

        #conf_layers += [ nn.Sequential(
        #            _conv_dw(in_channels, in_channels, stride=1, padding=1, expand_ratio=1, three_conv=False),
        #            _conv_dw(in_channels, box*num_classes, stride=1,padding=1, expand_ratio=1, three_conv=False))]
        loc_layers += [ _conv_dw(in_channels, box*4, stride=1,padding=1, expand_ratio=1, three_conv=False) ]
        conf_layers += [ _conv_dw(in_channels, box*num_classes, stride=1,padding=1, expand_ratio=1, three_conv=False) ]
    return base, extra_layers, (loc_layers, conf_layers)


# based on the implementation in https://github.com/tensorflow/models/blob/master/research/object_detection/models/feature_map_generators.py#L213
# when the expand_ratio is 1, the implemetation is nearly same. Since the shape is always change, I do not add the shortcut as what mobilenetv2 did.
def _conv_dw(inp, oup, stride=1, padding=0, expand_ratio=1, three_conv=True):
    if three_conv:
        return nn.Sequential(
            # pw
            nn.Conv2d(inp, oup * expand_ratio, 1, bias=False),
            nn.BatchNorm2d(oup * expand_ratio),
            nn.ReLU6(inplace=True),
            # dw
            nn.Conv2d(oup * expand_ratio, oup * expand_ratio, 3, stride, padding, groups=oup * expand_ratio, bias=False),
            nn.BatchNorm2d(oup * expand_ratio),
            nn.ReLU6(inplace=True),
            # pw-linear
            nn.Conv2d(oup * expand_ratio, oup, 1, bias=False),
            nn.BatchNorm2d(oup),
        )
    else:
        return nn.Sequential(
            # dw
            nn.Conv2d(inp , inp , 3, stride, padding, groups=inp, bias=False),
            nn.BatchNorm2d(inp),
            nn.ReLU6(inplace=True),
            # pw-linear
            nn.Conv2d(inp, oup, 1, 1, bias=False),
            nn.BatchNorm2d(oup),
        )

def build_ssd_lite(base, feature_layer, mbox, num_classes, conf_distr):
    base_, extras_, head_ = add_extras(base(), feature_layer, mbox, num_classes)
    return SSDLite(base_, extras_, head_, feature_layer, num_classes, conf_distr )
