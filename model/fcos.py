from .head import ClsCntRegHead
from .backbone.resnet import resnet101
import torch.nn as nn
from .loss import GenTargets, LOSS, coords_fmap2orig
import torch
from .config import DefaultConfig
from .mlfpn import M2Det, build_net
from .cc import model 

class FCOS(nn.Module):
    
    def __init__(self, config = None):
        super().__init__()
        if config is None:
            config = DefaultConfig
        
        self.backbone = resnet101(pretrained = True,
                                  if_include_top = False)
        self.mlfpn = build_net(config = model['m2det_config'])
        self.head = ClsCntRegHead(config.fpn_out_channels,
                                  config.class_num,
                                  config.use_GN_head,
                                  config.cnt_on_reg,
                                  config.prior
                                  )
        self.config = config
    
    def train(self, mode = True):
        super().train(mode = True)
        
        def freeze_bn(module):
            if isinstance(module, nn.BatchNorm2d):
                module.eval()
            classname = module.__class__.__name__
            
            if classname.find('BatchNorm') != -1:
                for p in module.parameters(): p.requires_grad = False
        
        if self.config.freeze_bn:
            self.apply(freeze_bn)
            print("INFO===>success frozen BN")
            
        if self.config.freeze_stage_1:
            self.backbone.freeze_stages(1)
            print("INFO===>success frozen backbone stage1")

    def forward(self,x):
        C3,C4,C5 = self.backbone(x)
        m2det = build_net('train', 
                size = 320, 
                config = model['m2det_config']
                )
        
        all_P = m2det(C3,C4)
        cls_logits,cnt_logits,reg_preds = self.head(all_P)
        
        return [cls_logits,cnt_logits,reg_preds]

class DetectHead(nn.Module):
    def __init__(self, score_threshold, nms_iou_threshold, max_detection_boxes_num, strides, config = None):
        super().__init__()
        self.score_threshold = score_threshold
        self.nms_iou_threshold = nms_iou_threshold
        self.max_detection_boxes_num = max_detection_boxes_num
        self.strides = strides
        
        if config is None:
            self.config = DefaultConfig
        else:
            self.config = config

    def forward(self,inputs):
        cls_logits,coords = self._reshape_cat_out(inputs[0],self.strides)                 # [batch_size,sum(_h*_w),class_num]
        cnt_logits,_ = self._reshape_cat_out(inputs[1],self.strides)                      # [batch_size,sum(_h*_w),1]
        reg_preds,_ = self._reshape_cat_out(inputs[2],self.strides)                       # [batch_size,sum(_h*_w),4]

        cls_preds = cls_logits.sigmoid_()
        cnt_preds = cnt_logits.sigmoid_()

        coords = coords.cuda() if torch.cuda.is_available() else coords

        cls_scores, cls_classes = torch.max(cls_preds, dim = -1)                          # [batch_size,sum(_h*_w)]
        if self.config.add_centerness:
            cls_scores = torch.sqrt(cls_scores*(cnt_preds.squeeze(dim = -1)))             # [batch_size,sum(_h*_w)]
        cls_classes = cls_classes + 1                                                     # [batch_size,sum(_h*_w)] 

        boxes = self._coords2boxes(coords, reg_preds)                                     # [batch_size,sum(_h*_w),4]

        # Select Top-k
        max_num = min(self.max_detection_boxes_num,cls_scores.shape[-1])
        topk_ind = torch.topk(cls_scores,max_num, dim = -1, largest = True, sorted = True)[1]  #[batch_size,max_num]
        _cls_scores = []
        _cls_classes = []
        _boxes = []
        for batch in range(cls_scores.shape[0]):
            _cls_scores.append(cls_scores[batch][topk_ind[batch]])                        # [max_num]
            _cls_classes.append(cls_classes[batch][topk_ind[batch]])                      # [max_num]
            _boxes.append(boxes[batch][topk_ind[batch]])                                  # [max_num,4]
        
        cls_scores_topk = torch.stack(_cls_scores,dim = 0)                                # [batch_size,max_num]
        cls_classes_topk = torch.stack(_cls_classes,dim = 0)                              # [batch_size,max_num]
        boxes_topk = torch.stack(_boxes, dim = 0)                                         # [batch_size,max_num,4]
        assert boxes_topk.shape[-1] == 4
        
        return self._post_process([cls_scores_topk, cls_classes_topk, boxes_topk])

    def _post_process(self,preds_topk):
        _cls_scores_post = []
        _cls_classes_post = []
        _boxes_post = []
        cls_scores_topk,cls_classes_topk,boxes_topk = preds_topk
        
        for batch in range(cls_classes_topk.shape[0]):
            mask = cls_scores_topk[batch] >= self.score_threshold
            _cls_scores_b = cls_scores_topk[batch][mask]
            _cls_classes_b = cls_classes_topk[batch][mask]
            _boxes_b = boxes_topk[batch][mask]
            nms_ind = self.batched_nms(_boxes_b, _cls_scores_b, _cls_classes_b, self.nms_iou_threshold)
            _cls_scores_post.append(_cls_scores_b[nms_ind])
            _cls_classes_post.append(_cls_classes_b[nms_ind])
            _boxes_post.append(_boxes_b[nms_ind])
        scores,classes,boxes = torch.stack(_cls_scores_post, dim = 0), torch.stack(_cls_classes_post, dim = 0), torch.stack(_boxes_post, dim = 0)
        
        return scores,classes,boxes
    
    @staticmethod
    def box_nms(boxes,scores,thr):

        if boxes.shape[0] == 0:
            return torch.zeros(0, device = boxes.device).long()
        assert boxes.shape[-1] == 4
        
        x1,y1,x2,y2 = boxes[:,0],boxes[:,1],boxes[:,2],boxes[:,3]
        areas = (x2-x1+1)*(y2-y1+1)
        order = scores.sort(0, descending = True)[1]
        keep = []
        while order.numel()>0:
            if order.numel() == 1:
                i = order.item()
                keep.append(i)
                break
            else:
                i = order[0].item()
                keep.append(i)
            
            xmin = x1[order[1:]].clamp(min=float(x1[i]))
            ymin = y1[order[1:]].clamp(min=float(y1[i]))
            xmax = x2[order[1:]].clamp(max=float(x2[i]))
            ymax = y2[order[1:]].clamp(max=float(y2[i]))
            inter = (xmax-xmin).clamp(min=0)*(ymax-ymin).clamp(min=0)
            iou = inter/(areas[i]+areas[order[1:]]-inter)
            idx = (iou<=thr).nonzero().squeeze()
            if idx.numel() == 0:
                break
            order = order[idx+1]
        
        return torch.LongTensor(keep)

    def batched_nms(self,boxes, scores, idxs, iou_threshold):
        
        if boxes.numel() == 0:
            return torch.empty((0,), 
                               dtype = torch.int64, 
                               device = boxes.device
                               )
        max_coordinate = boxes.max()
        offsets = idxs.to(boxes) * (max_coordinate + 1)
        boxes_for_nms = boxes + offsets[:, None]
        keep = self.box_nms(boxes_for_nms, scores, iou_threshold)
        return keep

    def _coords2boxes(self,coords,offsets):
        x1y1 = coords[None,:,:]-offsets[...,:2]
        x2y2 = coords[None,:,:]+offsets[...,2:]                         #[batch_size,sum(_h*_w),2]
        boxes = torch.cat([x1y1,x2y2],dim = -1)                         #[batch_size,sum(_h*_w),4]
        return boxes


    def _reshape_cat_out(self,inputs,strides):
        batch_size = inputs[0].shape[0]
        c = inputs[0].shape[1]
        out = []
        coords = []
        
        for pred,stride in zip(inputs,strides):
            pred = pred.permute(0,2,3,1)
            coord = coords_fmap2orig(pred,stride).to(device = pred.device)
            pred = torch.reshape(pred,[batch_size,-1,c])
            out.append(pred)
            coords.append(coord)
        return torch.cat(out,dim = 1), torch.cat(coords, dim = 0)

class ClipBoxes(nn.Module):
    def __init__(self):
        super().__init__()
    
    def forward(self,batch_imgs,batch_boxes):
        batch_boxes = batch_boxes.clamp_(min = 0)
        h,w = batch_imgs.shape[2:]
        batch_boxes[...,[0,2]] = batch_boxes[...,[0,2]].clamp_(max = w-1)
        batch_boxes[...,[1,3]] = batch_boxes[...,[1,3]].clamp_(max = h-1)
        
        return batch_boxes

        
class FCOSDetector(nn.Module):
    def __init__(self,mode = "training", config = None):
        super().__init__()
        if config is None:
            config = DefaultConfig
        
        self.mode = mode
        self.fcos_body = FCOS(config=config)
        
        if mode == "training":
            self.target_layer = GenTargets(strides = config.strides, 
                                           limit_range = config.limit_range
                                           )
            self.loss_layer = LOSS()
        
        elif mode == "inference":
            self.detection_head = DetectHead(config.score_threshold, 
                                             config.nms_iou_threshold,
                                            config.max_detection_boxes_num, 
                                            config.strides, 
                                            config
                                            )
            self.clip_boxes = ClipBoxes()
        
    
    def forward(self, inputs):

        if self.mode == "training":
            batch_imgs, batch_boxes, batch_classes = inputs
            out = self.fcos_body(batch_imgs)
            targets = self.target_layer([out,batch_boxes,batch_classes])
            losses = self.loss_layer([out,targets])
            return losses
        
        elif self.mode == "inference":
            batch_imgs = inputs
            out = self.fcos_body(batch_imgs)
            scores,classes,boxes = self.detection_head(out)
            boxes = self.clip_boxes(batch_imgs,boxes)
            return scores, classes, boxes