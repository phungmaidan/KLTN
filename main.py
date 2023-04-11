import time
import datetime
import numpy as np
import os
import os.path as osp
import sys
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.optim import lr_scheduler
from torch.distributions import Bernoulli
import h5py
from tabulate import tabulate

from utils import Logger, read_json, write_json, save_checkpoint
from rewards import compute_reward
from configs import get_config
from summarizer_module import *
import vsum_tools

def TrainModel(config, model, dataset, optimizer, train_keys, use_gpu, start_epoch):
    print("==> Start training")
    start_time = time.time()
    model.train()
    baselines = {key: 0. for key in train_keys}
    reward_writers = {key: [] for key in train_keys}

    for epoch in range(start_epoch, config.max_epoch):
        idxs = np.arange(len(train_keys))
        np.random.shuffle(idxs)

        for idx in idxs:
            key = train_keys[idx]
            seq = dataset[key]['features'][...] # sequence of features, (seq_len, dim)
            seq = torch.from_numpy(seq).unsqueeze(0) # input shape (1, seq_len, dim)
            if use_gpu: seq = seq.cuda()
            seq = seq.squeeze(0).to("cpu")
            probs = model(seq)
            probs = probs.clone().detach().requires_grad_(True).unsqueeze(0)

            cost = config.beta * (probs.mean() - 0.5)**2
            m = Bernoulli(probs)
            epis_rewards = []
            for _ in range(config.num_episode):
                actions = m.sample()
                log_probs = m.log_prob(actions)
                reward = compute_reward(seq, actions, use_gpu=use_gpu)
                expected_reward = log_probs.mean() * (reward - baselines[key])
                cost -= expected_reward
                epis_rewards.append(reward.item())

            optimizer.zero_grad()
            cost.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            optimizer.step()
            baselines[key] = 0.9 * baselines[key] + 0.1 * np.mean(epis_rewards)
            reward_writers[key].append(np.mean(epis_rewards))

        epoch_reward = np.mean([reward_writers[key][epoch] for key in train_keys])
        print("epoch {}/{}\t reward {}\t".format(epoch+1, config.max_epoch, epoch_reward))

    write_json(reward_writers, osp.join(config.save_dir, 'rewards.json'))

    elapsed = round(time.time() - start_time)
    elapsed = str(datetime.timedelta(seconds=elapsed))
    print("Finished. Total elapsed time (h:m:s): {}".format(elapsed))


def TestModel(config, model, dataset, test_keys, use_gpu):
    print("==> Test")
    with torch.no_grad():
        model.eval()
        fms = []
        eval_metric = 'avg' if config.video_type == 'tvsum' else 'max'

        if config.verbose:
            table = [["No.", "Video", "F-score"]]

        if config.save_results:
            h5_res = h5py.File(osp.join(config.save_dir, 'result.h5'), 'w')

        for key_idx, key in enumerate(test_keys):
            seq = dataset[key]['features'][...]
            seq = torch.from_numpy(seq).unsqueeze(0)
            if use_gpu:
                seq = seq.cuda()
            seq = seq.squeeze(0).to("cpu")
            probs = model(seq)
            probs = probs.unsqueeze(0)
            probs = probs.cpu().squeeze().numpy()

            cps = dataset[key]['change_points'][...]
            num_frames = dataset[key]['n_frames'][()]
            nfps = dataset[key]['n_frame_per_seg'][...].tolist()
            positions = dataset[key]['picks'][...]
            user_summary = dataset[key]['user_summary'][...]

            machine_summary = vsum_tools.generate_summary(probs, cps, num_frames, nfps, positions)
            fm, _, _ = vsum_tools.evaluate_summary(machine_summary, user_summary, eval_metric)
            fms.append(fm)

            if config.verbose:
                table.append([key_idx+1, key, "{:.1%}".format(fm)])

            if config.save_results:
                h5_res.create_dataset(key + '/score', data=probs)
                h5_res.create_dataset(key + '/machine_summary', data=machine_summary)
                h5_res.create_dataset(key + '/gtscore', data=dataset[key]['gtscore'][...])
                h5_res.create_dataset(key + '/fm', data=fm)

    if config.verbose:
        print(tabulate(table))

    if config.save_results:
        h5_res.close()

    mean_fm = np.mean(fms)
    print("Average F-score {:.1%}".format(mean_fm))



if __name__ == '__main__':
    config = get_config(mode='train')
    
    torch.manual_seed(config.seed)
    os.environ['CUDA_VISIBLE_DEVICES'] = config.gpu
    use_gpu = torch.cuda.is_available()
    if config.use_cpu: use_gpu = False

    if not config.evaluate:
        sys.stdout = Logger(osp.join(config.save_dir, 'log_train.txt'))
    else:
        sys.stdout = Logger(osp.join(config.save_dir, 'log_test.txt'))
    print("==========\n{}:\n==========".format(config))

    if use_gpu:
        print("Currently using GPU {}".format(config.gpu))
        cudnn.benchmark = True
        torch.cuda.manual_seed_all(config.seed)
    else:
        print("Currently using CPU")

    print("Initialize dataset {}".format(config.dataset))
    dataset = h5py.File(config.dataset, 'r')
    num_videos = len(dataset.keys())
    splits = read_json(config.split)
    assert config.split_id < len(splits), "split_id (got {}) exceeds {}".format(config.split_id, len(splits))
    split = splits[config.split_id]
    train_keys = split['train_keys']
    test_keys = split['test_keys']
    print("# total videos {}. # train videos {}. # test videos {}".format(num_videos, len(train_keys), len(test_keys)))

    print("Initialize model")
    model = Summarizer(input_size=config.input_size, output_size=config.input_size,block_size=60).to("cpu")
    print("Model size: {:.5f}M".format(sum(p.numel() for p in model.parameters())/1000000.0))

    optimizer = torch.optim.Adam(model.parameters(), lr=config.lr, weight_decay=config.weight_decay)
    if config.stepsize > 0:
        scheduler = lr_scheduler.StepLR(optimizer, step_size=config.stepsize, gamma=config.gamma)

    if config.resume:
        print("Loading checkpoint from '{}'".format(config.resume))
        checkpoint = torch.load(config.resume)
        model.load_state_dict(checkpoint)
    else:
        start_epoch = 0

    if use_gpu:
        model = nn.DataParallel(model).cuda()

    if config.evaluate:
        print("Evaluate only")
        TestModel(model, dataset, test_keys, use_gpu)
    else:
        TrainModel(config, model, dataset, optimizer, train_keys, use_gpu, start_epoch)
        TestModel(config, model, dataset, test_keys, use_gpu)

    model_state_dict = model.module.state_dict() if use_gpu else model.state_dict()
    model_save_path = osp.join(config.save_dir, 'model_epoch' + str(config.max_epoch) + '.pth.tar')
    save_checkpoint(model_state_dict, model_save_path)
    print("Model saved to {}".format(model_save_path))
