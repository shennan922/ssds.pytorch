from __future__ import print_function
import numpy as np
import os
import sys
import cv2
import datetime
import random
import pickle
import json

import torch
import torch.backends.cudnn as cudnn
from torch.autograd import Variable
import torch.optim as optim
from torch.optim import lr_scheduler
import torch.utils.data as data
import torch.nn.init as init

from tensorboardX import SummaryWriter

from lib.layers import *
from lib.utils.timer import Timer
from lib.utils.data_augment import preproc
from lib.modeling.model_builder import create_model
from lib.dataset.dataset_factory import load_data
from lib.utils.config_parse import cfg
from lib.utils.eval_utils import *
from lib.utils.visualize_utils import *
from lib.utils.box_utils import *

class Solver(object):
    """
    A wrapper class for the training process
    """
    def __init__(self):
        self.cfg = cfg

        # Load data
        print('===> Loading data')
        self.train_loader = load_data(cfg.DATASET, 'train') if 'train' in cfg.PHASE else None
        self.eval_loader = load_data(cfg.DATASET, 'eval') if 'eval' in cfg.PHASE else None
        self.test_loader = load_data(cfg.DATASET, 'test') if 'test' in cfg.PHASE else None
        self.visualize_loader = load_data(cfg.DATASET, 'visualize') if 'visualize' in cfg.PHASE else None

        if self.train_loader and hasattr(self.train_loader.dataset, "num_classes"):
            cfg.POST_PROCESS.NUM_CLASSES = cfg.MATCHER.NUM_CLASSES=cfg.MODEL.NUM_CLASSES=self.train_loader.dataset.num_classes
        elif self.eval_loader and hasattr(self.eval_loader.dataset, "num_classes"):
            cfg.POST_PROCESS.NUM_CLASSES = cfg.MATCHER.NUM_CLASSES=cfg.MODEL.NUM_CLASSES=self.eval_loader.dataset.num_classes
        elif self.test_loader and hasattr(self.test_loader.dataset, "num_classes"):
            cfg.POST_PROCESS.NUM_CLASSES = cfg.MATCHER.NUM_CLASSES=cfg.MODEL.NUM_CLASSES = self.test_loader.dataset.num_classes
        elif self.visualize_loader and hasattr(self.visualize_loader.dataset, "num_classes"):
            cfg.POST_PROCESS.NUM_CLASSES = cfg.MATCHER.NUM_CLASSES=cfg.MODEL.NUM_CLASSES = self.visualize_loader.dataset.num_classes

         # Build model
        print('===> Building model, num_classes is '+str(cfg.MODEL.NUM_CLASSES))

        self.model, self.priorbox = create_model(cfg.MODEL,cfg.LOSS.CONF_DISTR)
        self.priors = Variable(self.priorbox.forward(), volatile=True)
        self.detector = Detect(cfg.POST_PROCESS, self.priors)

        # Utilize GPUs for computation
        self.use_gpu = torch.cuda.is_available()
        if self.use_gpu:
            print('Utilize GPUs for computation')
            print('Number of GPU available', torch.cuda.device_count())
            self.model.cuda()
            self.priors.cuda()
            if torch.cuda.device_count() > 1:
                print('-----DataParallel-----------')
                self.model = torch.nn.DataParallel(self.model)
                self.model.cuda()
                #self.dp_model = torch.nn.DataParallel(self.model)
                #self.model = torch.nn.DataParallel(self.model).module
                #self.model = self.dp_model.module

            cudnn.benchmark = True

        # Print the model architecture and parameters
        #print('Model architectures:\n{}\n'.format(self.model))

        # print('Parameters and size:')
        # for name, param in self.model.named_parameters():
        #     print('{}: {}'.format(name, list(param.size())))

        # print trainable scope
        print('Trainable scope: {}'.format(cfg.TRAIN.TRAINABLE_SCOPE))
        trainable_param = self.trainable_param(cfg.TRAIN.TRAINABLE_SCOPE)
        self.optimizer = self.configure_optimizer(trainable_param, cfg.TRAIN.OPTIMIZER)
        self.exp_lr_scheduler = self.configure_lr_scheduler(self.optimizer, cfg.TRAIN.LR_SCHEDULER)
        self.max_epochs = cfg.TRAIN.MAX_EPOCHS

        # metric
        #self.criterion = MultiBoxLoss(cfg.MATCHER, self.priors, self.use_gpu)
        self.criterion = FocalLoss(cfg.MATCHER, self.priors, self.use_gpu, cfg.LOSS)

        # Set the logger
        self.writer = SummaryWriter(log_dir=cfg.LOG_DIR)
        self.output_dir = cfg.EXP_DIR
        self.checkpoint = cfg.RESUME_CHECKPOINT
        self.pretrained= cfg.PRETRAINED
        self.checkpoint_prefix = cfg.CHECKPOINTS_PREFIX


    def save_checkpoints(self, epochs, iters=None):
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)
        if iters:
            filename = self.checkpoint_prefix + '_epoch_{:d}_iter_{:d}'.format(epochs, iters) + '.pth'
        else:
            filename = self.checkpoint_prefix + '_epoch_{:d}'.format(epochs) + '.pth'
        filename = os.path.join(self.output_dir, filename)
        real_model = self.get_real_model()
        torch.save(real_model.state_dict(), filename)
        with open(os.path.join(self.output_dir, 'checkpoint_list.txt'), 'a') as f:
            f.write('epoch {epoch:d}: {filename}\n'.format(epoch=epochs, filename=filename))
        print('Wrote snapshot to: {:s}'.format(filename))

        # TODO: write relative cfg under the same page

    def resume_checkpoint(self, resume_checkpoint):
        if resume_checkpoint == '' or not os.path.isfile(resume_checkpoint):
            print(("=> no checkpoint found at '{}'".format(resume_checkpoint)))
            return False
        print(("=> loading checkpoint '{:s}'".format(resume_checkpoint)))
        checkpoint = torch.load(resume_checkpoint)

        # print("=> Weigths in the checkpoints:")
        # print([k for k, v in list(checkpoint.items())])

        # remove the module in the parrallel model
        if 'module.' in list(checkpoint.items())[0][0]:
            pretrained_dict = {'.'.join(k.split('.')[1:]): v for k, v in list(checkpoint.items())}
            checkpoint = pretrained_dict

        elif "features." in list(checkpoint.items())[0][0]:
            #classification file,
            new_model_state = {}
            pretrained_dict = {'base.'+'.'.join(k.split('.')[1:]): v for k, v in list(checkpoint.items())}
            checkpoint = pretrained_dict

        # change the name of the weights which exists in other model
        # change_dict = {
        #         'conv1.weight':'base.0.weight',
        #         'bn1.running_mean':'base.1.running_mean',
        #         'bn1.running_var':'base.1.running_var',
        #         'bn1.bias':'base.1.bias',
        #         'bn1.weight':'base.1.weight',
        #         }
        # for k, v in list(checkpoint.items()):
        #     for _k, _v in list(change_dict.items()):
        #         if _k == k:
        #             new_key = k.replace(_k, _v)
        #             checkpoint[new_key] = checkpoint.pop(k)
        # change_dict = {'layer1.{:d}.'.format(i):'base.{:d}.'.format(i+4) for i in range(20)}
        # change_dict.update({'layer2.{:d}.'.format(i):'base.{:d}.'.format(i+7) for i in range(20)})
        # change_dict.update({'layer3.{:d}.'.format(i):'base.{:d}.'.format(i+11) for i in range(30)})
        # for k, v in list(checkpoint.items()):
        #     for _k, _v in list(change_dict.items()):
        #         if _k in k:
        #             new_key = k.replace(_k, _v)
        #             checkpoint[new_key] = checkpoint.pop(k)

        resume_scope = self.cfg.TRAIN.RESUME_SCOPE
        # extract the weights based on the resume scope
        if resume_scope != '':
            pretrained_dict = {}
            for k, v in list(checkpoint.items()):
                for resume_key in resume_scope.split(','):
                    if resume_key in k:
                        pretrained_dict[k] = v
                        break
            checkpoint = pretrained_dict

        real_model = self.get_real_model()
        #real_model = self.model.module if self.model.module else self.model
        pretrained_dict = {k: v for k, v in checkpoint.items() if k in real_model.state_dict()}
        # print("=> Resume weigths:")
        # print([k for k, v in list(pretrained_dict.items())])

        checkpoint = real_model.state_dict()

        unresume_dict = set(checkpoint)-set(pretrained_dict)
        if len(unresume_dict) != 0:
            print("=> UNResume weigths:")
            print(unresume_dict)

        checkpoint.update(pretrained_dict)
        return real_model.load_state_dict(checkpoint)


    def find_previous(self):
        if not os.path.exists(os.path.join(self.output_dir, 'checkpoint_list.txt')):
            return False
        with open(os.path.join(self.output_dir, 'checkpoint_list.txt'), 'r') as f:
            lineList = f.readlines()
        epoches, resume_checkpoints = [list() for _ in range(2)]
        for line in lineList:
            epoch = int(line[line.find('epoch ') + len('epoch '): line.find(':')])
            checkpoint = line[line.find(':') + 2:-1]
            epoches.append(epoch)
            resume_checkpoints.append(checkpoint)
        return epoches, resume_checkpoints

    def weights_init(self, m):
        for key in m.state_dict():
            if key.split('.')[-1] == 'weight':
                if 'conv' in key:
                    init.kaiming_normal(m.state_dict()[key], mode='fan_out')
                if 'bn' in key:
                    m.state_dict()[key][...] = 1
            elif key.split('.')[-1] == 'bias':
                m.state_dict()[key][...] = 0


    def initialize(self):
        # TODO: ADD INIT ways
        # raise ValueError("Fan in and fan out can not be computed for tensor with less than 2 dimensions")
        # for module in self.cfg.TRAIN.TRAINABLE_SCOPE.split(','):
        #     if hasattr(self.model, module):
        #         getattr(self.model, module).apply(self.weights_init)
        if self.checkpoint:
            print('Loading initial model weights from detection file{:s}'.format(self.checkpoint))
            self.resume_checkpoint(self.checkpoint)
        elif self.pretrained:
            print('Loading initial model weights from classification file {:s}'.format(self.pretrained))
            self.resume_checkpoint(self.pretrained)

        start_epoch = 0
        return start_epoch
    def get_real_model(self):
        if hasattr(self.model,"module"):
            return self.model.module
        else:
            return  self.model

    def trainable_param(self, trainable_scope):
        real_model = self.get_real_model()
        for param in real_model.parameters():
            param.requires_grad = False

        trainable_param = []
        for module in trainable_scope.split(','):
            if hasattr(real_model, module):
                # print(getattr(self.model, module))
                for param in getattr(real_model, module).parameters():
                    param.requires_grad = True
                trainable_param.extend(getattr(real_model, module).parameters())

        return trainable_param

    def train_model(self):
        previous = self.find_previous()
        if previous:
            start_epoch = previous[0][-1]
            self.resume_checkpoint(previous[1][-1])
        else:
            start_epoch = self.initialize()

        # export graph for the model, onnx always not works
        # self.export_graph()

        # warm_up epoch
        warm_up = self.cfg.TRAIN.LR_SCHEDULER.WARM_UP_EPOCHS
        for epoch in iter(range(start_epoch+1, self.max_epochs+1)):
            #learning rate
            sys.stdout.write('\r'+str(datetime.datetime.now())+': Epoch {epoch:d}/{max_epochs:d}:\n'.format(epoch=epoch, max_epochs=self.max_epochs))
            if epoch > warm_up:
                self.exp_lr_scheduler.step(epoch-warm_up)
            if 'train' in cfg.PHASE:
                self.train_epoch(self.model, self.train_loader, self.optimizer, self.criterion, self.writer, epoch, self.use_gpu)
            if 'eval' in cfg.PHASE:
                self.eval_epoch(self.model, self.eval_loader, self.detector, self.criterion, self.writer, epoch, self.use_gpu)
            if 'test' in cfg.PHASE:
                self.test_epoch(self.model, self.test_loader, self.detector, self.output_dir, self.use_gpu)
            if 'visualize' in cfg.PHASE:
                self.visualize_epoch(self.model, self.visualize_loader, self.priorbox, self.writer, epoch,  self.use_gpu)

            if epoch % cfg.TRAIN.CHECKPOINTS_EPOCHS == 0:
                self.save_checkpoints(epoch)

    def test_model(self):
        previous = self.find_previous()
        if previous:
            for epoch, resume_checkpoint in zip(previous[0], previous[1]):
                if self.cfg.TEST.TEST_SCOPE[0] <= epoch <= self.cfg.TEST.TEST_SCOPE[1]:
                    sys.stdout.write('\rEpoch {epoch:d}/{max_epochs:d}:\n'.format(epoch=epoch, max_epochs=self.cfg.TEST.TEST_SCOPE[1]))
                    self.resume_checkpoint(resume_checkpoint)
                    if 'eval' in cfg.PHASE:
                        self.eval_epoch(self.model, self.eval_loader, self.detector, self.criterion, self.writer, epoch, self.use_gpu)
                    if 'test' in cfg.PHASE:
                        self.test_epoch(self.model, self.test_loader, self.detector, self.output_dir , self.use_gpu)
                    if 'visualize' in cfg.PHASE:
                        self.visualize_epoch(self.model, self.visualize_loader, self.priorbox, self.writer, epoch,  self.use_gpu)
        else:
            sys.stdout.write('\rCheckpoint {}:\n'.format(self.checkpoint))
            self.resume_checkpoint(self.checkpoint)
            if 'eval' in cfg.PHASE:
                self.eval_epoch(self.model, self.eval_loader, self.detector, self.criterion, self.writer, 0, self.use_gpu)
            if 'test' in cfg.PHASE:
                self.test_epoch(self.model, self.test_loader, self.detector, self.output_dir , self.use_gpu)
            if 'visualize' in cfg.PHASE:
                self.visualize_epoch(self.model, self.visualize_loader, self.priorbox, self.writer, 0,  self.use_gpu)

    def restore_model_from_checkpoint(self):
        previous = self.find_previous()
        if previous:
            for epoch, resume_checkpoint in zip(previous[0], previous[1]):
                if epoch == self.cfg.TEST.TEST_SCOPE[1]:
                    sys.stdout.write('\rEpoch {epoch:d}/{max_epochs:d}:\n'.format(epoch=epoch, max_epochs=self.cfg.TEST.TEST_SCOPE[1]))
                    self.resume_checkpoint(resume_checkpoint)
        else:
            sys.stdout.write('\rCheckpoint {}:\n'.format(self.checkpoint))
            self.resume_checkpoint(self.checkpoint)


    def export_onnx(self, onnx_file):
        model= self.get_real_model()
        model.onnx_export = True
        model.eval()
        # #STEPS: [[16, 16], [32, 32], [64, 64], [100, 100]]
        # #SIZES: [0.035, 0.08, 0.16, 0.32, 0.6]
        # #ASPECT_RATIOS: [
        # #    [1.82940672, 1.31881404, 0.49710597], [1.82940672, 1.31881404, 0.49710597],
        # #    [1.82940672, 1.31881404, 0.49710597], [1.82940672, 1.31881404, 0.49710597]
        # #]
        # steps=[ step[0] for step in cfg.MODEL.STEPS]
        # steps= torch.tensor(steps, dtype=torch.int32)
        # sizes= torch.tensor(cfg.MODEL.SIZES)
        # aspect_ratios= torch.Tensor(cfg.MODEL.ASPECT_RATIOS[0])
        # model.set_anchor_setting(steps,
        #                          sizes,
        #                          aspect_ratios)

        #model.train(False)
        #images = torch.randn(1, 3, 533, 400)
        images = torch.randn(1, 3, 800 , 600)
        #images = torch.randn(1, 800 , 600, 3)
        # detection_model = nn.Sequential(
        #     nn.transpose(0,3, 1,2,3)
        #     model
        # )
        torch.onnx.export(model, images, onnx_file, verbose=True,
                          output_names=['np_loc','np_score'])
        with open(onnx_file+'.npnn.header', 'w') as f:
            print("Version: MBV2_1", file=f)
            print("StepScale: %s"% (" ".join([str(i) for i in self.cfg.MODEL.SIZES])) , file=f)
            print("AspectRatio: %s"% (" ".join([str(i) for i in self.cfg.MODEL.ASPECT_RATIOS[0]])), file=f)
            print("SkuNum: %d" %(model.num_classes-1), file=f)
            print("Content:",  file=f)
        #torch.onnx.export(model, images, onnx_file, verbose=True)


    def train_epoch(self, model, data_loader, optimizer, criterion, writer, epoch, use_gpu):
        model.train()

        _t_all2 = Timer()
        _t_all2.tic()
        epoch_size = len(data_loader)
        batch_iterator = iter(data_loader)

        loc_loss = 0
        conf_loss = 0
        _t = Timer()
        _t_all = Timer()
        for iteration in iter(range((epoch_size))):
            _t_all.tic()
            images, targets = next(batch_iterator)
            if use_gpu:
                images = Variable(images.cuda(),requires_grad=False)
                targets = [Variable(anno.cuda(), requires_grad=False) for anno in targets]
            else:
                images = Variable(images)
                targets = [Variable(anno, requires_grad=False) for anno in targets]
            _t.tic()
            # forward
            out = model(images, phase='train')

            # backprop
            optimizer.zero_grad()
            loss_l, loss_c = criterion(out, targets)

            # some bugs in coco train2017. maybe the annonation bug.
            if loss_l.item() == float("Inf"):
                continue

            loss = loss_l + loss_c
            loss.backward()
            optimizer.step()

            time = _t.toc()
            loc_loss += loss_l.item()
            conf_loss += loss_c.item()
            time_all=_t_all.toc()
            # log per iter
            log = '\r==>Train: || {iters:d}/{epoch_size:d} in {time:.3f}/{all_time:.3f}s,  [{prograss}] || loc_loss: {loc_loss:.4f} cls_loss: {cls_loss:.4f}\r'.format(
                    prograss='#'*int(round(10*iteration/epoch_size)) + '-'*int(round(10*(1-iteration/epoch_size))), iters=iteration, epoch_size=epoch_size,
                    time=time, all_time=time_all, loc_loss=loss_l.item(), cls_loss=loss_c.item())

            sys.stdout.write(log)
            sys.stdout.flush()

        _t_all2.toc()
        # log per epoch
        sys.stdout.write('\r')
        sys.stdout.flush()
        lr = optimizer.param_groups[0]['lr']
        log = '\r==>Train: || Total_time: {time:.3f}s || loc_loss: {loc_loss:.4f} conf_loss: {conf_loss:.4f} || lr: {lr:.6f}\n'.format(lr=lr,
                time=_t_all2.total_time, loc_loss=loc_loss/epoch_size, conf_loss=conf_loss/epoch_size)
        sys.stdout.write(log)
        sys.stdout.flush()

        # log for tensorboard
        writer.add_scalar('Train/loc_loss', loc_loss/epoch_size, epoch)
        writer.add_scalar('Train/conf_loss', conf_loss/epoch_size, epoch)
        writer.add_scalar('Train/lr', lr, epoch)

    def check_priors(self, images, targets, writer):
        """targets is the list , len is batch no"""
        mean = torch.Tensor(self.cfg.DATASET.PIXEL_MEANS).cpu()
        priors = self.priors
        for idx, truths in enumerate(targets):
            truths=truths[:,:4].cuda()
            overlaps = jaccard(
                truths,
                point_form(priors)
            )
            # [1,num_objects] best prior for each ground truth
            best_prior_overlap, best_prior_idx = overlaps.max(1, keepdim=True)
            # [1,num_priors] best ground truth for each prior
            #best_truth_overlap, best_truth_idx = overlaps.max(0, keepdim=True)

            best_prior_overlap=best_prior_overlap.squeeze(1)
            best_prior_idx = best_prior_idx.squeeze(1)
            mask = best_prior_overlap < 0.4

            number = mask.sum()
            if number <=0:
                continue

            print(">>>>find bad anchor: %d <<<<"%(number))

            image = images[idx]
            image = image.permute(1,2,0)
            image = image + mean
            image = image.byte()
            npimage = image.numpy()
            npimage = npimage[..., ::-1]
            #bad_anchors=priors[mask]
            bad_box =  truths[mask]

            bad_prior_idx=best_prior_idx[mask]
            bad_prior = point_form(priors[bad_prior_idx])

            def draw_bbox(npimage, bbxs_tensor, color=(0, 255, 0)):
                npimage = npimage.copy()
                bbxs_tensor[:, ::2] *= npimage.shape[1]
                bbxs_tensor[:, 1::2] *= npimage.shape[0]
                bbxs = bbxs_tensor.cpu().numpy().astype(np.int32)
                for bbx in bbxs:
                    cv2.rectangle(npimage, (bbx[0], bbx[1]), (bbx[2], bbx[3]), color, 2)
                return npimage


            image_show = draw_bbox(npimage, bad_box)
            image_show = draw_bbox(image_show, bad_prior, color=(0,0,0))
            writer.add_image('check_anchor_box/input_image', image_show, 0, dataformats='HWC')


    def eval_epoch(self, model, data_loader, detector, criterion, writer, epoch, use_gpu):
        model.eval()

        epoch_size = len(data_loader)
        batch_iterator = iter(data_loader)

        loc_loss = 0
        conf_loss = 0
        _t = Timer()

        label = [list() for _ in range(model.num_classes)]
        gt_label = [list() for _ in range(model.num_classes)]
        score = [list() for _ in range(model.num_classes)]
        size = [list() for _ in range(model.num_classes)]
        npos = [0] * model.num_classes

        for iteration in iter(range((epoch_size))):
        # for iteration in iter(range((10))):
            images, targets = next(batch_iterator)
            #self.check_priors(images, targets, writer)
            if use_gpu:
                images = Variable(images.cuda())
                targets = [Variable(anno.cuda(), volatile=True) for anno in targets]
            else:
                images = Variable(images)
                targets = [Variable(anno, volatile=True) for anno in targets]


            _t.tic()
            # forward
            out = model(images, phase='train')

            # loss
            loss_l, loss_c = criterion(out, targets)

            out = (out[0], model.softmax(out[1].view(-1, model.num_classes)))

            # detect
            detections = detector.forward(out)

            time = _t.toc()

            # evals
            label, score, npos, gt_label = cal_tp_fp(detections, targets, label, score, npos, gt_label)
            size = cal_size(detections, targets, size)
            loc_loss += loss_l.item()
            conf_loss += loss_c.item()

            # log per iter
            log = '\r==>Eval: || {iters:d}/{epoch_size:d} in {time:.3f}s [{prograss}] || loc_loss: {loc_loss:.4f} cls_loss: {cls_loss:.4f}\r'.format(
                    prograss='#'*int(round(10*iteration/epoch_size)) + '-'*int(round(10*(1-iteration/epoch_size))), iters=iteration, epoch_size=epoch_size,
                    time=time, loc_loss=loss_l.item(), cls_loss=loss_c.item())

            sys.stdout.write(log)
            sys.stdout.flush()

        # eval mAP
        prec, rec, ap = cal_pr(label, score, npos)

        # log per epoch
        sys.stdout.write('\r')
        sys.stdout.flush()
        log = '\r==>Eval: || Total_time: {time:.3f}s || loc_loss: {loc_loss:.4f} conf_loss: {conf_loss:.4f} || mAP: {mAP:.6f}\n'.format(mAP=ap,
                time=_t.total_time, loc_loss=loc_loss/epoch_size, conf_loss=conf_loss/epoch_size)
        sys.stdout.write(log)
        sys.stdout.flush()

        # log for tensorboard
        writer.add_scalar('Eval/loc_loss', loc_loss/epoch_size, epoch)
        writer.add_scalar('Eval/conf_loss', conf_loss/epoch_size, epoch)
        writer.add_scalar('Eval/mAP', ap, epoch)
        viz_pr_curve(writer, prec, rec, epoch)
        viz_archor_strategy(writer, size, gt_label, epoch)


    def detect_one_image(self, np_image):
        self._detect_one_image(self.model, np_image, self.test_loader.dataset.preproc, self.detector,  self.use_gpu)

    def _detect_one_image(selfself, model, np_image, preproc, detector, use_gpu=True):
        model.eval()
        #model.onnx_export = True
        num_classes = detector.num_classes

        img = np_image
        scale = [img.shape[1], img.shape[0], img.shape[1], img.shape[0]]
        if use_gpu:
            images = Variable(preproc(img)[0].unsqueeze(0).cuda(), requires_grad=False)
        else:
            images = Variable(preproc(img)[0].unsqueeze(0), requires_grad=False)

        out = model(images, phase='eval')

        # detect
        detections = detector.forward(out)
        _scores=[]
        _labels=[]
        _coords=[]
        batch = 0
        for j in range(1, num_classes):
            for det in detections[0][j]:
                if det[0] > 0.45:
                    d = det.cpu().numpy()
                    score, box = d[0], d[1:]
                    box *= scale
                    _labels.append(j-1)
                    _coords.append(box)
                    _scores.append(score)

        COLORS = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
        FONT = cv2.FONT_HERSHEY_SIMPLEX
        print("test single image:   "+ str(_coords.__len__()))
        for label, score, coord in zip(_labels, _scores, _coords):
             cv2.rectangle(img, (int(coord[0]), int(coord[1])), (int(coord[2]), int(coord[3])), COLORS[label % 3], 2)
             #labelname = get_label_name(label)
             cv2.putText(img, '{label}: {score:.3f}'.format(label=label, score=score),
                         (int(coord[0]), int(coord[1])+60), FONT, 0.5, COLORS[label % 3], 2)
        #
        #
        # cv2.imwrite('/tmp/ddd_result.jpg', image)



    def test_epoch(self, model, data_loader, detector, output_dir, use_gpu):
        model.eval()
        #model.onnx_export = True
        dataset = data_loader.dataset
        num_images = len(dataset)
        num_classes = detector.num_classes
        all_boxes = [[[] for _ in range(num_images)] for _ in range(num_classes)]
        empty_array = np.transpose(np.array([[],[],[],[],[]]),(1,0))

        _t = Timer()

        for i in iter(range((num_images))):
            img = dataset.pull_image(i)
            scale = [img.shape[1], img.shape[0], img.shape[1], img.shape[0]]
            if use_gpu:
                images = Variable(dataset.preproc(img)[0].unsqueeze(0).cuda(), requires_grad=False)
            else:
                images = Variable(dataset.preproc(img)[0].unsqueeze(0), requires_grad=False)

            _t.tic()
            # forward
            out = model(images, phase='eval')

            # detect
            detections = detector.forward(out)

            time = _t.toc()

            # TODO: make it smart:
            for j in range(1, num_classes):
                cls_dets = list()
                for det in detections[0][j]:
                    if det[0] > 0:
                        d = det.cpu().numpy()
                        score, box = d[0], d[1:]
                        box *= scale
                        box = np.append(box, score)
                        cls_dets.append(box)
                if len(cls_dets) == 0:
                    cls_dets = empty_array
                all_boxes[j][i] = np.array(cls_dets)

            # log per iter
            log = '\r==>Test: || {iters:d}/{epoch_size:d} in {time:.3f}s [{prograss}]\r'.format(
                    prograss='#'*int(round(10*i/num_images)) + '-'*int(round(10*(1-i/num_images))), iters=i, epoch_size=num_images,
                    time=time)
            sys.stdout.write(log)
            sys.stdout.flush()

        # write result to pkl
        with open(os.path.join(output_dir, 'detections.pkl'), 'wb') as f:
            pickle.dump(all_boxes, f, pickle.HIGHEST_PROTOCOL)

        # currently the COCO dataset do not return the mean ap or ap 0.5:0.95 values
        print('Evaluating detections')
        data_loader.dataset.evaluate_detections(all_boxes, output_dir)


    def visualize_epoch(self, model, data_loader, priorbox, writer, epoch, use_gpu):
        model.eval()

        img_index = random.randint(0, len(data_loader.dataset)-1)
        #img_index = 1

        # get img
        image = data_loader.dataset.pull_image(img_index)
        anno = data_loader.dataset.pull_anno(img_index)

        # visualize archor box
        viz_prior_box(writer, priorbox, image, epoch)

        # get preproc
        preproc = data_loader.dataset.preproc
        preproc.add_writer(writer, epoch)
        # preproc.p = 0.6

        # preproc image & visualize preprocess prograss
        images = Variable(preproc(image, anno)[0].unsqueeze(0), volatile=True)
        if use_gpu:
            images = images.cuda()

        # visualize feature map in base and extras
        base_out = viz_module_feature_maps(writer, model.base, images, module_name='base', epoch=epoch)
        extras_out = viz_module_feature_maps(writer, model.extras, base_out, module_name='extras', epoch=epoch)
        # visualize feature map in feature_extractors
        viz_feature_maps(writer, model(images, 'feature'), module_name='feature_extractors', epoch=epoch)

        model.train()
        images.requires_grad = True
        images.volatile=False
        base_out = viz_module_grads(writer, model, model.base, images, images, preproc.means, module_name='base', epoch=epoch)

        # TODO: add more...


    def configure_optimizer(self, trainable_param, cfg):
        if cfg.OPTIMIZER == 'sgd':
            optimizer = optim.SGD(trainable_param, lr=cfg.LEARNING_RATE,
                        momentum=cfg.MOMENTUM, weight_decay=cfg.WEIGHT_DECAY)
        elif cfg.OPTIMIZER == 'rmsprop':
            optimizer = optim.RMSprop(trainable_param, lr=cfg.LEARNING_RATE,
                        momentum=cfg.MOMENTUM, alpha=cfg.MOMENTUM_2, eps=cfg.EPS, weight_decay=cfg.WEIGHT_DECAY)
        elif cfg.OPTIMIZER == 'adam':
            optimizer = optim.Adam(trainable_param, lr=cfg.LEARNING_RATE,
                        betas=(cfg.MOMENTUM, cfg.MOMENTUM_2), eps=cfg.EPS, weight_decay=cfg.WEIGHT_DECAY)
        else:
            AssertionError('optimizer can not be recognized.')
        return optimizer


    def configure_lr_scheduler(self, optimizer, cfg):
        if cfg.SCHEDULER == 'step':
            scheduler = lr_scheduler.StepLR(optimizer, step_size=cfg.STEPS[0], gamma=cfg.GAMMA)
        elif cfg.SCHEDULER == 'multi_step':
            scheduler = lr_scheduler.MultiStepLR(optimizer, milestones=cfg.STEPS, gamma=cfg.GAMMA)
        elif cfg.SCHEDULER == 'exponential':
            scheduler = lr_scheduler.ExponentialLR(optimizer, gamma=cfg.GAMMA)
        elif cfg.SCHEDULER == 'SGDR':
            scheduler = lr_scheduler.CosineAnnealingLR(optimizer, T_max=cfg.MAX_EPOCHS)
        else:
            AssertionError('scheduler can not be recognized.')
        return scheduler


    def export_graph(self):
        self.model.train(False)
        dummy_input = Variable(torch.randn(1, 3, cfg.MODEL.IMAGE_SIZE[0], cfg.MODEL.IMAGE_SIZE[1])).cuda()
        # Export the model
        torch_out = torch.onnx._export(self.model,             # model being run
                                       dummy_input,            # model input (or a tuple for multiple inputs)
                                       "graph.onnx",           # where to save the model (can be a file or file-like object)
                                       export_params=True)     # store the trained parameter weights inside the model file
        # if not os.path.exists(cfg.EXP_DIR):
        #     os.makedirs(cfg.EXP_DIR)
        # self.writer.add_graph(self.model, (dummy_input, ))

def load_template_json(label_seq, json_path):
    with open(json_path, "rb") as fp:
        str1 = fp.read().decode('utf-8')
        labels = json.loads(str1)
        label_def = labels.get('categories', None)[0].get("skus")
        label_id = label_def[label_seq].get("id")
        return label_id

def init_checkpoint():
    s = Solver()
    s.restore_model_from_checkpoint()
    return s

def create_npjson(s, image, json_path,use_gpu = True):
    bboxes = list()
    model=s.model

    preproc= s.test_loader.dataset.preproc
    detector = s.detector
    
    model.eval()
    # model.onnx_export = True
    num_classes = detector.num_classes
    im_height, im_width, _ = image.shape
    img = image
    scale = [img.shape[1], img.shape[0], img.shape[1], img.shape[0]]
    if use_gpu:
        images = Variable(preproc(img)[0].unsqueeze(0).cuda(), requires_grad=False)
    else:
        images = Variable(preproc(img)[0].unsqueeze(0), requires_grad=False)

    out = model(images, phase='eval')

    # detect
    detections = detector.forward(out)
    _scores = []
    _labels = []
    _coords = []
    batch = 0
    for j in range(1, num_classes):
        for det in detections[0][j]:
            if det[0] > 0.45:
                d = det.cpu().numpy()
                score, box = d[0], d[1:]
                box *= scale
                _labels.append(j - 1)
                _coords.append(box)
                _scores.append(score)

    COLORS = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    FONT = cv2.FONT_HERSHEY_SIMPLEX
    #print("newjson:   "+ str(_coords.__len__()))
    for label, score, coord in zip(_labels, _scores, _coords):
        label_id = load_template_json(label, json_path)
        xmin, ymin, xmax, ymax = coord
        bbox = {
            # {
            'x': xmin* 1,
            'y': ymin* 1,
            'w': (xmax - xmin)* 1,
            'h': (ymax - ymin)* 1,
            'id': label_id
            # },
            # 'category': category_index[classes[i]]['name'],
            # 'score': float(scores[i])
        }
        bboxes.append(bbox)

    return bboxes


def train_model():
    s = Solver()
    s.train_model()
    return True

def test_model():
    s = Solver()
    s.test_model()
    return True

def test_image(np_image):
    s = Solver()
    s.restore_model_from_checkpoint()
    s.detect_one_image(np_image)
    return True

def export_onnx_model(onnx_file):
    s = Solver()
    s.restore_model_from_checkpoint()
    s.export_onnx(onnx_file)
    return True

