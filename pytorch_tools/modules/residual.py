import math
import torch
import torch.nn as nn
from functools import partial
from .activated_batch_norm import ABN
from .activations import activation_from_name

# from pytorch_tools.modules import ABN
# from pytorch_tools.modules import activation_from_name
from pytorch_tools.modules import BlurPool
from pytorch_tools.modules import FastGlobalAvgPool2d
from pytorch_tools.utils.misc import make_divisible
from pytorch_tools.modules import SpaceToDepth


def conv3x3(in_planes, out_planes, stride=1, groups=1, dilation=1, bias=False):
    """3x3 convolution with padding"""
    return nn.Conv2d(
        in_planes,
        out_planes,
        kernel_size=3,
        stride=stride,
        padding=dilation,
        groups=groups,
        bias=bias,
        dilation=dilation,
    )


def conv1x1(in_planes, out_planes, stride=1, bias=False):
    """1x1 convolution"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=1, stride=stride, bias=bias)


class SEModule(nn.Module):
    def __init__(self, channels, reduction_channels, norm_act="relu"):
        super(SEModule, self).__init__()

        self.pool = FastGlobalAvgPool2d()
        # authors of original paper DO use bias
        self.fc1 = conv1x1(channels, reduction_channels, bias=True)
        self.act1 = activation_from_name(norm_act)
        self.fc2 = conv1x1(reduction_channels, channels, bias=True)

    def forward(self, x):
        x_se = self.pool(x)
        x_se = self.fc1(x_se)
        x_se = self.act1(x_se)
        x_se = self.fc2(x_se)
        return x * x_se.sigmoid()


class ECAModule(nn.Module):
    """Efficient Channel Attention
    This implementation is different from the paper. I've removed all hyperparameters and
    use fixed kernel size of 3. If you think it may be better to use different k_size - feel free to open an issue.

    Ref: ECA-Net: Efficient Channel Attention for Deep Convolutional Neural Networks
    https://arxiv.org/abs/1910.03151

    """

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.pool = FastGlobalAvgPool2d()
        self.conv = nn.Conv1d(1, 1, kernel_size=3, padding=1, bias=False)

    def forward(self, x):
        x_s = self.pool(x)
        x_s = self.conv(x_s.view(x.size(0), 1, -1))
        x_s = x_s.view(x.size(0), -1, 1, 1).sigmoid()
        return x * x_s.expand_as(x)


class SSEModule(nn.Module):
    """Spatial Excitation Block (sSE)
    Attention which excites certain locations in spatial domain instead of channel. Works better for segmentation than SE
    Ref: Recalibrating Fully Convolutional Networks with Spatial and Channel ‘Squeeze & Excitation’ Blocks
    https://arxiv.org/abs/1808.08127
    """

    def __init__(self, in_ch, *args):  # parse additional args for compatability
        super().__init__()
        self.conv = conv1x1(in_ch, 1, bias=True)

    def forward(self, x):
        return x * self.conv(x).sigmoid()


class SCSEModule(nn.Module):
    """Idea from Spatial and Channel ‘Squeeze & Excitation’ (scSE)
    ECA is proven to work better than (c)SE so i'm using ECA + sSE instead of original cSE + sSE

    NOTE: This modules also performs additional conv to return the same number of channels as before

    Ref: Recalibrating Fully Convolutional Networks with Spatial and Channel ‘Squeeze & Excitation’ Blocks
    https://arxiv.org/abs/1808.08127

    Ref: ECA-Net: Efficient Channel Attention for Deep Convolutional Neural Networks
    https://arxiv.org/abs/1910.03151
    """

    def __init__(self, in_ch, *args):  # parse additional args for compatability
        super().__init__()
        self.sse = SSEModule(in_ch)
        self.cse = ECAModule()
        self.reduction_conv = conv1x1(in_ch * 2, in_ch, bias=True)  # use bias because there is no BN after

    def forward(self, x):
        return self.reduction_conv(torch.cat([self.sse(x), self.cse(x)], dim=1))


def get_attn(attn_type):
    """Get attention by name
    Args:
        attn_type (Uniont[str, None]): Attention type. Supported:
            `se` - Squeeze and Excitation
            `eca` - Efficient Channel Attention
            `sse` - Spatial Excitation
            `scse` - Spatial and Channel ‘Squeeze & Excitation’
            None - no attention
    """
    ATT_TO_MODULE = {"se": SEModule, "eca": ECAModule, "sse": SSEModule, "scse": SCSEModule}
    if attn_type is None:
        return nn.Identity
    else:
        return ATT_TO_MODULE[attn_type.lower()]


class DepthwiseSeparableConv(nn.Sequential):
    """Depthwise separable conv with BN after depthwise & pointwise."""

    def __init__(
        self, in_channels, out_channels, stride=1, dilation=1, norm_layer=ABN, norm_act="relu", use_norm=True
    ):
        modules = [
            conv3x3(in_channels, in_channels, stride=stride, groups=in_channels, dilation=dilation),
            # bias is needed for EffDet because in head conv is separated from normalization
            conv1x1(in_channels, out_channels, bias=True),
            norm_layer(out_channels, activation=norm_act) if use_norm else nn.Identity(),
        ]
        super().__init__(*modules)


class InvertedResidual(nn.Module):
    def __init__(
        self,
        in_channels,
        out_channels,
        dw_kernel_size=3,
        stride=1,
        dilation=1,
        attn_type=None,
        expand_ratio=1.0,  # expansion
        keep_prob=1,  # drop connect param
        noskip=False,
        norm_layer=ABN,
        norm_act="relu",
    ):
        super().__init__()
        mid_chs = make_divisible(in_channels * expand_ratio)
        self.has_residual = (in_channels == out_channels and stride == 1) and not noskip
        self.has_expansion = expand_ratio != 1
        if self.has_expansion:
            self.conv_pw = conv1x1(in_channels, mid_chs)
            self.bn1 = norm_layer(mid_chs, activation=norm_act)

        self.conv_dw = nn.Conv2d(
            mid_chs,
            mid_chs,
            dw_kernel_size,
            stride=stride,
            groups=mid_chs,
            dilation=dilation,
            bias=False,
            padding=dilation * (dw_kernel_size - 1) // 2,
        )
        self.bn2 = norm_layer(mid_chs, activation=norm_act)
        # some models like MobileNet use mid_chs here instead of in_channels. But I don't care for now
        self.se = get_attn(attn_type)(mid_chs, in_channels // 4, norm_act)
        self.conv_pw1 = conv1x1(mid_chs, out_channels)
        self.bn3 = norm_layer(out_channels, activation="identity")
        self.drop_connect = DropConnect(keep_prob) if keep_prob < 1 else nn.Identity()

    def forward(self, x):
        residual = x
        if self.has_expansion:
            x = self.conv_pw(x)
            x = self.bn1(x)
        x = self.conv_dw(x)
        x = self.bn2(x)
        x = self.se(x)
        x = self.conv_pw1(x)
        x = self.bn3(x)

        if self.has_residual:
            x = self.drop_connect(x) + residual
        return x


class DropConnect(nn.Module):
    """Randomply drops samples from input.
    Implements idea close to one from https://arxiv.org/abs/1603.09382"""

    def __init__(self, keep_prob):
        super().__init__()
        self.keep_prob = keep_prob

    def forward(self, x):
        if not self.training:
            return x
        batch_size = x.size(0)
        random_tensor = self.keep_prob
        random_tensor += torch.rand([batch_size, 1, 1, 1], dtype=x.dtype, device=x.device)
        binary_tensor = torch.floor(random_tensor)
        output = x / self.keep_prob * binary_tensor
        return output

    def extra_repr(self):
        return f"keep_prob={self.keep_prob:.2f}"


class BasicBlock(nn.Module):
    expansion = 1

    def __init__(
        self,
        inplanes,
        planes,
        stride=1,
        downsample=None,
        groups=1,
        base_width=64,
        attn_type=None,
        dilation=1,
        norm_layer=ABN,
        norm_act="relu",
        antialias=False,
        keep_prob=1,
    ):
        super(BasicBlock, self).__init__()
        antialias = antialias and stride == 2
        assert groups == 1, "BasicBlock only supports groups of 1"
        assert base_width == 64, "BasicBlock doest not support changing base width"
        outplanes = planes * self.expansion
        conv1_stride = 1 if antialias else stride
        self.conv1 = conv3x3(inplanes, planes, conv1_stride, groups, dilation)
        self.bn1 = norm_layer(planes, activation=norm_act)
        self.conv2 = conv3x3(planes, outplanes)
        self.bn2 = norm_layer(outplanes, activation="identity")
        self.se_module = get_attn(attn_type)(outplanes, planes // 4)
        self.final_act = activation_from_name(norm_act)
        self.downsample = downsample
        self.blurpool = BlurPool(channels=planes) if antialias else nn.Identity()
        self.antialias = antialias
        self.drop_connect = DropConnect(keep_prob) if keep_prob < 1 else nn.Identity()

    def forward(self, x):
        residual = x

        if self.downsample is not None:
            residual = self.downsample(x)

        out = self.conv1(x)
        out = self.bn1(out)
        # Conv(s=2)->BN->Relu(s=1) => Conv(s=1)->BN->Relu(s=1)->BlurPool(s=2)
        if self.antialias:
            out = self.blurpool(out)
        out = self.conv2(out)
        # avoid 2 inplace ops by chaining into one long op. Needed for inplaceabn
        out = self.drop_connect(self.se_module(self.bn2(out))) + residual
        return self.final_act(out)


# This class is from torchvision with many (many) modifications
# it's not very intuitive. Check this article if you want to understand the code more
# https://medium.com/@erikgaas/resnet-torchvision-bottlenecks-and-layers-not-as-they-seem-145620f93096
class Bottleneck(nn.Module):
    expansion = 4

    def __init__(
        self,
        inplanes,
        planes,
        stride=1,
        downsample=None,
        groups=1,
        base_width=64,
        attn_type=None,
        dilation=1,
        norm_layer=ABN,
        norm_act="relu",
        antialias=False,
        keep_prob=1,  # for drop connect
    ):
        super(Bottleneck, self).__init__()
        antialias = antialias and stride == 2
        width = int(math.floor(planes * (base_width / 64)) * groups)
        outplanes = planes * self.expansion

        self.conv1 = conv1x1(inplanes, width)
        self.bn1 = norm_layer(width, activation=norm_act)
        conv2_stride = 1 if antialias else stride
        self.conv2 = conv3x3(width, width, conv2_stride, groups, dilation)
        self.bn2 = norm_layer(width, activation=norm_act)
        self.conv3 = conv1x1(width, outplanes)
        self.bn3 = norm_layer(outplanes, activation="identity")
        self.se_module = get_attn(attn_type)(outplanes, planes // 4)
        self.final_act = activation_from_name(norm_act)
        self.downsample = downsample
        self.blurpool = BlurPool(channels=width) if antialias else nn.Identity()
        self.antialias = antialias
        self.drop_connect = DropConnect(keep_prob) if keep_prob < 1 else nn.Identity()

    def forward(self, x):
        residual = x

        if self.downsample is not None:
            residual = self.downsample(x)

        out = self.conv1(x)
        out = self.bn1(out)

        # Conv(s=2)->BN->Relu(s=1) => Conv(s=1)->BN->Relu(s=1)->BlurPool(s=2)
        out = self.conv2(out)
        out = self.bn2(out)
        if self.antialias:
            out = self.blurpool(out)

        out = self.conv3(out)
        # avoid 2 inplace ops by chaining into one long op
        out = self.drop_connect(self.se_module(self.bn3(out))) + residual
        return self.final_act(out)


# TResnet models use slightly modified versions of BasicBlock and Bottleneck
# need to adjust for it
class TBasicBlock(BasicBlock):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.final_act = nn.ReLU(inplace=True)
        self.bn1.activation_param = 1e-3  # needed for loading weights
        if not kwargs.get("attn_type") == "se":
            return
        planes = kwargs["planes"]
        self.se_module = SEModule(planes, max(planes // 4, 64))


class TBottleneck(Bottleneck):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.final_act = nn.ReLU(inplace=True)
        self.bn1.activation_param = 1e-3  # needed for loading weights
        self.bn2.activation_param = 1e-3
        if not kwargs.get("attn_type") == "se":
            return
        planes = kwargs["planes"]
        reduce_planes = max(planes * self.expansion // 8, 64)
        self.se_module = SEModule(planes, reduce_planes)

    # use se after 2nd conv instead of 3rd
    def forward(self, x):
        residual = x

        if self.downsample is not None:
            residual = self.downsample(x)

        out = self.conv1(x)
        out = self.bn1(out)

        # Conv(s=2)->BN->Relu(s=1) => Conv(s=1)->BN->Relu(s=1)->BlurPool(s=2)
        out = self.conv2(out)
        out = self.bn2(out)
        if self.antialias:
            out = self.blurpool(out)

        out = self.se_module(out)

        out = self.conv3(out)
        # avoid 2 inplace ops by chaining into one long op
        out = self.drop_connect(self.bn3(out)) + residual
        return self.final_act(out)


## DarkNet blocks
class DarkBasicBlock(nn.Module):
    """Basic Block for DarkNet family models"""

    def __init__(
        self,
        in_channels,
        out_channels,
        bottle_ratio=0.5,
        attn_type=None,
        norm_layer=ABN,
        norm_act="leaky_relu",
        keep_prob=1,
    ):
        super().__init__()
        mid_channels = int(in_channels * bottle_ratio)
        self.bn1 = norm_layer(mid_channels, activation=norm_act)
        self.conv1 = conv1x1(in_channels, mid_channels)
        self.bn2 = norm_layer(out_channels, activation=norm_act)
        self.conv2 = conv3x3(mid_channels, out_channels, groups=32)
        # In original DarkNet they have activation after second BN but the most recent papers
        # (Mobilenet v2 for example) show that it is better to use linear here
        # out_channels // 4 is for SE attention. other attentions don't use second parameter
        self.attention = get_attn(attn_type)(out_channels, out_channels // 4)
        self.drop_connect = DropConnect(keep_prob) if keep_prob < 1 else nn.Identity()

    def forward(self, x):
        # preAct
        out = self.bn1(x)
        out = self.conv1(x)
        out = self.bn2(out)
        out = self.conv2(out)
        # out = self.bn3(out)
        # out = self.conv3(out)
        out = self.drop_connect(self.attention(out)) + x
        return out


class CSPDarkBasicBlock(nn.Module):
    """Idea from https://github.com/WongKinYiu/CrossStagePartialNetworks
    But implementaion is different. This block divides input into two passes only one part through bottleneck
    """

    def __init__(
        self, in_channels, out_channels, attn_type=None, norm_layer=ABN, norm_act="leaky_relu", keep_prob=1,
    ):
        super().__init__()
        mid_channels = int(in_channels * bottle_ratio)
        self.conv1 = conv1x1(in_channels, mid_channels)
        self.bn1 = norm_layer(mid_channels, activation=norm_act)
        self.conv2 = conv3x3(mid_channels, out_channels)
        # In original DarkNet they have activation after second BN but the most recent papers
        # (Mobilenet v2 for example) show that it is better to use linear here
        self.bn2 = norm_layer(out_channels, activation="identity")
        # out_channels // 4 is for SE attention. other attentions don't use second parameter
        self.attention = get_attn(attn_type)(out_channels, out_channels // 4)
        self.drop_connect = DropConnect(keep_prob) if keep_prob < 1 else nn.Identity()

    def forward(self, x):
        x1, x2 = torch.chunk(x, chunks=2, dim=1)

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.conv2(out)
        # avoid 2 inplace ops by chaining into one long op. Needed for inplaceabn
        out = self.drop_connect(self.attention(self.bn2(out))) + x
        return out


class SimpleBottleneck(nn.Module):
    """Simple Bottleneck without downsample support"""

    def __init__(
        self,
        in_chs,
        mid_chs,
        out_chs,
        stride=1,
        # attn_type=None,
        groups=1,
        groups_width=None,
        norm_layer=ABN,
        norm_act="relu",
        keep_prob=1,  # for drop connect
    ):
        super().__init__()
        groups = mid_chs // groups_width if groups_width else groups
        self.conv1 = conv1x1(in_chs, mid_chs)
        self.bn1 = norm_layer(mid_chs, activation=norm_act)
        self.conv2 = conv3x3(mid_chs, mid_chs, stride=stride, groups=groups)  # dilation)
        self.bn2 = norm_layer(mid_chs, activation=norm_act)
        self.conv3 = conv1x1(mid_chs, out_chs)
        self.bn3 = norm_layer(out_chs, activation="identity")
        self.has_residual = in_chs == out_chs and stride == 1
        # self.se_module = get_attn(attn_type)(outplanes, planes // 4)
        # self.drop_connect = DropConnect(keep_prob) if keep_prob < 1 else nn.Identity()

    def forward(self, x):
        out = self.conv1(x)
        out = self.bn1(out)
        out = self.conv2(out)
        out = self.bn2(out)
        out = self.conv3(out)
        # avoid 2 inplace ops by chaining into one long op
        if self.has_residual:
            out = self.bn3(out) + x
        else:
            out = self.bn3(out)
        return out


class SimplePreActBottleneck(nn.Module):
    """Simple Bottleneck with preactivation"""

    def __init__(
        self,
        in_chs,
        mid_chs,
        out_chs,
        stride=1,
        norm_layer=ABN,
        norm_act="relu",
        groups=1,
        groups_width=None,
        # keep_prob=1,  # for drop connect
    ):
        super().__init__()
        groups = mid_chs // groups_width if groups_width else groups
        self.bn1 = norm_layer(in_chs, activation=norm_act)
        self.conv1 = conv1x1(in_chs, mid_chs)
        self.bn2 = norm_layer(mid_chs, activation=norm_act)
        self.conv2 = conv3x3(mid_chs, mid_chs, stride=stride, groups=groups)
        self.bn3 = norm_layer(mid_chs, activation=norm_act)
        # last conv is not followed by bn, but anyway bias here makes it slightly worse (on Imagenet)
        self.conv3 = conv1x1(mid_chs, out_chs)
        self.has_residual = in_chs == out_chs and stride == 1

    def forward(self, x):
        out = self.bn1(x)
        out = self.conv1(out)
        out = self.bn2(out)
        out = self.conv2(out)
        out = self.bn3(out)
        if self.has_residual:
            out = self.conv3(out) + x
        else:
            out = self.conv3(out)
        return out


class SimpleStage(nn.Module):
    """One stage in DarkNet models. It consists of first transition conv (with stride == 2) and
    DarkBasicBlock repeated num_blocks times
    Args:
        in_channels (int): input channels for this stage
        out_channels (int): output channels for this stage
        num_blocks (int): number of residual blocks in stage
        stride (int): stride for first convolution
        bottle_ratio (float): how much channels are reduced inside blocks
        antialias (bool): flag to apply gaussiian smoothing before conv with stride 2
    
    Ref: TODO: add 

    """

    def __init__(
        self,
        in_chs,
        out_chs,
        num_blocks,
        stride=2,
        bottle_ratio=0.5,
        antialias=False,
        block_fn=DarkBasicBlock,
        attn_type=None,
        norm_layer=ABN,
        norm_act="leaky_relu",
        keep_prob=1,
        **block_kwargs,
    ):
        super().__init__()
        norm_kwarg = dict(norm_layer=norm_layer, norm_act=norm_act, **block_kwargs)  # this is dirty
        mid_chs = max(int(out_chs * bottle_ratio), 64)
        layers = [block_fn(in_chs=in_chs, mid_chs=mid_chs, out_chs=out_chs, stride=stride, **norm_kwarg)]
        block_kwargs = dict(in_chs=out_chs, mid_chs=mid_chs, out_chs=out_chs, **norm_kwarg)
        layers.extend([block_fn(**block_kwargs) for _ in range(num_blocks - 1)])
        self.blocks = nn.Sequential(*layers)

    def forward(self, x):
        return self.blocks(x)


class CrossStage(nn.Module):
    def __init__(
        self,
        in_chs,
        out_chs,
        num_blocks,
        stride=2,
        bottle_ratio=0.5,
        antialias=False,
        block_fn=SimpleBottleneck,
        attn_type=None,
        norm_layer=ABN,
        norm_act="leaky_relu",
        keep_prob=1,
        csp_block_ratio=0.5,  # how many channels go to blocks
        x2_transition=True,
        **block_kwargs,
    ):
        super().__init__()
        extra_kwarg = dict(norm_layer=norm_layer, norm_act=norm_act, **block_kwargs)
        self.first_layer = block_fn(
            in_chs=in_chs, mid_chs=out_chs, out_chs=out_chs, stride=stride, **extra_kwarg
        )
        block_chs = int(csp_block_ratio * out_chs)  # todo: maybe change to make divizable or hardcode values
        extra_kwarg.update(in_chs=block_chs, mid_chs=block_chs, out_chs=block_chs)
        self.blocks = nn.Sequential(*[block_fn(**extra_kwarg) for _ in range(num_blocks - 1)])
        # using identity activation in transition conv. the idea is the same as in Linear Bottleneck
        # maybe need to test this design choice later. maybe I can simply remove this transition
        self.x2_transition = nn.Sequential(
            conv1x1(block_chs, block_chs), norm_layer(block_chs, activation="identity")
        ) if x2_transition else nn.Identity()
        self.csp_block_ratio = csp_block_ratio

    def forward(self, x):
        x = self.first_layer(x)
        if self.csp_block_ratio == 0.5:
            x1, x2 = torch.chunk(x, chunks=2, dim=1)
        elif self.csp_block_ratio == 0.75:
            x1, x2, x3, x4 = torch.chunk(x, chunks=4, dim=1)
            x2 = torch.cat([x2, x3, x4], dim=1)
        x2 = self.blocks(x2)
        x2 = self.x2_transition(x2)
        out = torch.cat([x1, x2], dim=1)
        # no explicit transition here. first conv in the next stage would perform transition
        return out
