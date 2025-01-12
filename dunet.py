'''
Adapted from fast.ai Dynamic Unet
https://github.com/fastai/fastai/blob/d3ef60a96cddf5b503361ed4c95d68dda4a873fc/fastai/models/unet.py
'''
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import numpy as np
from torchvision import models as modelzoo


def get_sfs_idxs(sfs, last=True):
    """
    Return the saved feature indexes that will be concatenated
    Inputs:
        sfs (list): saved features by hook function, in other words intermediate activations
        last (bool): whether to concatenate only last different activation, or all from the encoder model
    """
    if last:
        feature_szs = [sfs_feats.features.size()[-1] for sfs_feats in sfs]
        sfs_idxs = list(np.where(np.array(feature_szs[:-1]) != np.array(feature_szs[1:]))[0])
        if feature_szs[0] != feature_szs[1]:
            sfs_idxs = [0] + sfs_idxs
    else:
        sfs_idxs = list(range(len(sfs)))
    return sfs_idxs


def conv_bn_relu(in_c, out_c, kernel_size, stride, padding):
    return [
        nn.Conv2d(in_c, out_c, kernel_size=kernel_size, stride=stride, padding=padding),
        nn.ReLU(),
        nn.BatchNorm2d(out_c)]


class UnetBlock(nn.Module):
    # TODO: ADAPT KERNEL SIZE, STRIDE AND PADDING SO THAT ANY SIZE DECAY WILL BE SUPPORTED
    def __init__(self, up_in_c, x_in_c):
        super().__init__()
        self.upconv = nn.ConvTranspose2d(up_in_c, up_in_c // 2, 2, 2)  # H, W -> 2H, 2W
        self.conv1 = nn.Conv2d(x_in_c + up_in_c // 2, (x_in_c + up_in_c // 2) // 2, 3, 1, 1)
        self.conv2 = nn.Conv2d((x_in_c + up_in_c // 2) // 2, (x_in_c + up_in_c // 2) // 2, 3, 1, 1)
        self.bn = nn.BatchNorm2d((x_in_c + up_in_c // 2) // 2)

    def forward(self, up_in, x_in):
        up_out = self.upconv(up_in)
        cat_x = torch.cat([up_out, x_in], dim=1)
        x = F.relu(self.conv1(cat_x))
        x = F.relu(self.conv2(x))
        return self.bn(x)


class SaveFeatures():
    """ Extract pretrained activations"""
    features = None
    def __init__(self, m): self.hook = m.register_forward_hook(self.hook_fn)
    def hook_fn(self, module, input, output): self.features = output
    def remove(self): self.hook.remove()


class DynamicUnet(nn.Module):
    """
    A dynamic implementation of Unet architecture, because calculating connections
    and channels suck!. When an encoder is passed, this network will
    automatically construct a decoder after the first single forward pass for any
    given encoder architecture.
    Decoder part is heavily based on the original Unet paper:
    https://arxiv.org/abs/1505.04597.
    Inputs:
        encoder(nn.Module): Preferably a pretrained model, such as VGG or ResNet
        last (bool): Whether to concat only last activation just before a size change
        n_classes (int): Number of classes to output in final step of decoder
    Important Note: If architecture directly reduces the dimension of an image as soon as the
    first forward pass then output size will not be same as the input size, e.g. ResNet.
    In order to resolve this problem architecture will add an additional extra conv transpose
    layer. Also, currently Dynamic Unet expects size change to be H,W -> H/2, W/2. This is
    not a problem for state-of-the-art architectures as they follow this pattern but it should
    be changed for custom encoders that might have a different size decay.
    """

    def __init__(self, encoder, last=True, n_classes=3):
        super().__init__()
        self.encoder = encoder
        self.n_children = len(list(encoder.children()))
        self.sfs = [SaveFeatures(encoder[i]) for i in range(self.n_children)]
        self.last = last
        self.n_classes = n_classes

    def forward(self, x):
        # get imsize
        imsize = x.size()[-2:]

        # encoder output
        x = F.relu(self.encoder(x))

        # initialize sfs_idxs, sfs_szs, middle_in_c and middle_conv only once
        if not hasattr(self, 'middle_conv'):
            self.sfs_szs = [sfs_feats.features.size() for sfs_feats in self.sfs]
            self.sfs_idxs = get_sfs_idxs(self.sfs, self.last)
            middle_in_c = self.sfs_szs[-1][1]
            middle_conv = nn.Sequential(*conv_bn_relu(middle_in_c, middle_in_c * 2, 3, 1, 1),
                                        *conv_bn_relu(middle_in_c * 2, middle_in_c, 3, 1, 1))
            self.middle_conv = middle_conv

        # middle conv
        x = self.middle_conv(x)

        # initialize upmodel, extra_block and 1x1 final conv
        if not hasattr(self, 'upmodel'):
            x_copy = Variable(x.data, requires_grad=False)
            upmodel = []
            for idx in self.sfs_idxs[::-1]:
                up_in_c, x_in_c = int(x_copy.size()[1]), int(self.sfs_szs[idx][1])
                unet_block = UnetBlock(up_in_c, x_in_c)
                upmodel.append(unet_block)
                x_copy = unet_block(x_copy, self.sfs[idx].features)
                self.upmodel = nn.Sequential(*upmodel)

            if imsize != self.sfs_szs[0][-2:]:
                extra_in_c = self.upmodel[-1].conv2.out_channels
                self.extra_block = nn.ConvTranspose2d(extra_in_c, extra_in_c, 2, 2)

            final_in_c = self.upmodel[-1].conv2.out_channels
            self.final_conv = nn.Conv2d(final_in_c, self.n_classes, 1)

        # run upsample
        for block, idx in zip(self.upmodel, self.sfs_idxs[::-1]):
            x = block(x, self.sfs[idx].features)
        if hasattr(self, 'extra_block'):
            x = self.extra_block(x)

        out = self.final_conv(x)
        out = out.sigmoid()
        return out


def get_dunet():
    '''
    Returns a DynamicUnet with Resnet34 as encoder
    '''
    print("DUNET needs E2D. Using resnet34 and applying sigmoid")
    rnet = modelzoo.resnet34(pretrained=True)
    rnet_clip = nn.Sequential(rnet.conv1, rnet.bn1, rnet.relu, rnet.layer1, rnet.layer2, rnet.layer3, rnet.layer4)
    dunet = DynamicUnet(rnet_clip, n_classes=1)
    estimuli = torch.ones(1, 3, 32, 32)
    dunet(estimuli.cpu())
    return dunet


if __name__ == "__main__":
    print("Testing dynamic unet")
    import cv2 as cv
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    print(device)
    rnet = modelzoo.resnet34(pretrained=True)
    rnet_clip = nn.Sequential(rnet.conv1, rnet.bn1, rnet.relu, rnet.layer1, rnet.layer2, rnet.layer3, rnet.layer4)
    dunet = DynamicUnet(rnet_clip, n_classes=1)
    inp = torch.ones(1, 3, 32, 32)  # initial stimuli? thats weird
    out = dunet(inp.cpu())
    dunet.cuda()
    test_input = torch.randn((10, 3, 32, 32)).cuda()
    gout = dunet(test_input).cpu()
    print(gout.shape)

    i = test_input.cpu().numpy()[0].transpose(1, 2, 0)
    o = gout.cpu().detach().numpy()[0].transpose(1, 2, 0)

    cv.imshow("i", i)
    cv.imshow("o", o)
    # print(dunet)
    cv.waitKey(1000)
