import torch
import numpy as np
import cv2

def sort_by_score(pred_boxes, pred_labels, pred_scores):
    score_seq = [(-score).argsort() for index, score in enumerate(pred_scores)]
    pred_boxes = [sample_boxes[mask] for sample_boxes, mask in zip(pred_boxes, score_seq)]
    pred_labels = [sample_boxes[mask] for sample_boxes, mask in zip(pred_labels, score_seq)]
    pred_scores = [sample_boxes[mask] for sample_boxes, mask in zip(pred_scores, score_seq)]
    return pred_boxes, pred_labels, pred_scores

def iou_2d(cubes_a, cubes_b):

    # expands dim
    cubes_a = np.expand_dims(cubes_a, axis = 1)                                 # [N,1,4]
    cubes_b = np.expand_dims(cubes_b, axis = 0)                                 # [1,M,4]
    overlap = np.maximum(0.0,
                         np.minimum(cubes_a[..., 2:], cubes_b[..., 2:]) -
                         np.maximum(cubes_a[..., :2], cubes_b[..., :2]))        # [N,M,(w,h)]

    # overlap
    overlap = np.prod(overlap, axis = -1)                                       # [N,M]

    # compute area
    area_a = np.prod(cubes_a[..., 2:] - cubes_a[..., :2], axis = -1)
    area_b = np.prod(cubes_b[..., 2:] - cubes_b[..., :2], axis = -1)

    # compute iou
    iou = overlap / (area_a + area_b - overlap)
    return iou

def _compute_ap(recall, precision):
    """
    ##### Reference repo: https://github.com/rbgirshick/py-faster-rcnn.
    """
    # correct AP calculation
    # first append sentinel values at the end
    mrec = np.concatenate(([0.], recall, [1.]))
    mpre = np.concatenate(([0.], precision, [0.]))

    # compute the precision envelope
    for i in range(mpre.size - 1, 0, -1):
        mpre[i - 1] = np.maximum(mpre[i - 1], mpre[i])

    # to calculate area under PR curve, look for points
    # where X axis (recall) changes value
    i = np.where(mrec[1:] != mrec[:-1])[0]

    # and sum (\Delta recall) * prec
    ap = np.sum((mrec[i + 1] - mrec[i]) * mpre[i + 1])
    
    return ap

def eval_ap_2d(gt_boxes,           # list of 2d array,shape[(a,(x1,y1,x2,y2)),(b,(x1,y1,x2,y2))...] 
               gt_labels,          # list of 1d array,shape[(a),(b)...],value is sparse label index
               pred_boxes,         # list of 2d array, shape[(m,(x1,y1,x2,y2)),(n,(x1,y1,x2,y2))...]
               pred_labels,        # list of 1d array,shape[(m),(n)...],value is sparse label index
               pred_scores,        # list of 1d array,shape[(m),(n)...]
               iou_thread, 
               num_cls):           # total number of class including background which is equal to 0

    all_ap = {}
    for label in range(num_cls)[1:]:
        # get samples with specific label
        true_label_loc = [sample_labels == label for sample_labels in gt_labels]
        gt_single_cls = [sample_boxes[mask] for sample_boxes, mask in zip(gt_boxes, true_label_loc)]

        pred_label_loc = [sample_labels == label for sample_labels in pred_labels]
        bbox_single_cls = [sample_boxes[mask] for sample_boxes, mask in zip(pred_boxes, pred_label_loc)]
        scores_single_cls = [sample_scores[mask] for sample_scores, mask in zip(pred_scores, pred_label_loc)]

        fp = np.zeros((0,))
        tp = np.zeros((0,))
        scores = np.zeros((0,))
        total_gts = 0
        # loop for each sample
        for sample_gts, sample_pred_box, sample_scores in zip(gt_single_cls, bbox_single_cls, scores_single_cls):
            total_gts = total_gts + len(sample_gts)
            assigned_gt = []  # one gt can only be assigned to one predicted bbox
            # loop for each predicted bbox
            for index in range(len(sample_pred_box)):
                scores = np.append(scores, sample_scores[index])
                if len(sample_gts) == 0:  # if no gts found for the predicted bbox, assign the bbox to fp
                    fp = np.append(fp, 1)
                    tp = np.append(tp, 0)
                    continue
                pred_box = np.expand_dims(sample_pred_box[index], axis = 0)
                iou = iou_2d(sample_gts, pred_box)
                gt_for_box = np.argmax(iou, axis = 0)
                max_overlap = iou[gt_for_box, 0]
                if max_overlap >= iou_thread and gt_for_box not in assigned_gt:
                    fp = np.append(fp, 0)
                    tp = np.append(tp, 1)
                    assigned_gt.append(gt_for_box)
                else:
                    fp = np.append(fp, 1)
                    tp = np.append(tp, 0)
        # sort by score
        indices = np.argsort(-scores)
        fp = fp[indices]
        tp = tp[indices]
        # compute cumulative false positives and true positives
        fp = np.cumsum(fp)
        tp = np.cumsum(tp)
        # compute recall and precision
        recall = tp / total_gts
        precision = tp / np.maximum(tp + fp, np.finfo(np.float64).eps)
        ap = _compute_ap(recall, precision)
        all_ap[label] = ap
        # print(recall, precision)
    
    return all_ap               # a dict containing average precision for each cls

if __name__=="__main__":
    from model.fcos_copy import FCOSDetector
    #from demo import convertSyncBNtoBN
    from dataset.VOC_dataset import VOCDataset
    

    eval_dataset = VOCDataset(root_dir = 'root_directory_path', resize_size = [800, 1333],
                               split = 'test', use_difficult = False, is_train = False, augment = None)
    print("INFO===>eval dataset has %d imgs"%len(eval_dataset))
    eval_loader = torch.utils.data.DataLoader(eval_dataset,
                                              batch_size = 1,
                                              shuffle = False,
                                              collate_fn = eval_dataset.collate_fn)

    model = FCOSDetector(mode = "inference")
    # model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
    # print("INFO===>success convert BN to SyncBN")
    model = torch.nn.DataParallel(model)
    model.load_state_dict(torch.load("./checkpoint/model_16.pth",
                                     map_location = torch.device('cpu')))
    # model = convertSyncBNtoBN(model)
    # print("INFO===>success convert SyncBN to BN")
    model = model.cuda().eval()
    print("===>success loading model")

    gt_boxes = []
    gt_classes = []
    pred_boxes = []
    pred_classes = []
    pred_scores = []
    num = 0
    
    for img,boxes,classes in eval_loader:
        with torch.no_grad():
            out = model(img.cuda())
            pred_boxes.append(out[2][0].cpu().numpy())
            pred_classes.append(out[1][0].cpu().numpy())
            pred_scores.append(out[0][0].cpu().numpy())
        gt_boxes.append(boxes[0].numpy())
        gt_classes.append(classes[0].numpy())
        num += 1
        print(num, end='\r')

    pred_boxes,pred_classes,pred_scores = sort_by_score(pred_boxes, pred_classes, pred_scores)
    all_AP = eval_ap_2d(gt_boxes, gt_classes, pred_boxes, pred_classes, pred_scores, 0.5, len(eval_dataset.CLASSES_NAME))
    print("all classes AP=====>\n")
    
    for key,value in all_AP.items():
        print('ap for {} is {}'.format(eval_dataset.id2name[int(key)],value))
    
    mAP = 0.
    for class_id,class_mAP in all_AP.items():
        mAP += float(class_mAP)
    mAP /= (len(eval_dataset.CLASSES_NAME)-1)
    
    print("mAP=====>%.3f\n"%mAP)