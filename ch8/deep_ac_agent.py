#!/usr/bin/env python
import numpy as np
import torch
from torch.distributions.multivariate_normal import MultivariateNormal
import torch.multiprocessing as mp
import torch.nn.functional as F
import gym
try:
    import roboschool
except ImportError:
    pass
from argparse import ArgumentParser
from datetime import datetime
from collections import namedtuple
from tensorboardX import SummaryWriter
from utils.params_manager import ParamsManager

parser = ArgumentParser("deep_ac_agent")
parser.add_argument("--env-name",
                    type= str,
                    default="CarRacing-v0",
                    metavar="ENV_ID")
parser.add_argument("--params-file",
                    type= str,
                    default="parameters.json",
                    metavar="PFILE.json")
args = parser.parse_args()
global_step_num = 0

params_manager= ParamsManager(args.params_file)
seed = params_manager.get_agent_params()['seed']  # With the intent to make the results reproducible
summary_file_path_prefix = params_manager.get_agent_params()['summary_file_path_prefix']
summary_file_path= summary_file_path_prefix + args.env_name + "_" + datetime.now().strftime("%y-%m-%d-%H-%M")
writer = SummaryWriter(summary_file_path)
# Export the parameters as json files to the log directory to keep track of the parameters used in each experiment
params_manager.export_env_params(summary_file_path + "/" + "env_params.json")
params_manager.export_agent_params(summary_file_path + "/" + "agent_params.json")
use_cuda = params_manager.get_agent_params()['use_cuda']
# Introduced in PyTorch 0.4
device = torch.device("cuda" if torch.cuda.is_available() and use_cuda else "cpu")
torch.manual_seed(seed)
np.random.seed(seed)
if torch.cuda.is_available() and use_cuda:
    torch.cuda.manual_seed_all(seed)

Transition = namedtuple("Transition", ["s", "value_s", "a", "log_prob_a"])


class ShallowActor(torch.nn.Module):
    def __init__(self, input_shape, output_shape):
        super(ShallowActor, self).__init__()
        self.layer1 = torch.nn.Sequential(torch.nn.Linear(input_shape[0], 32),
                                          torch.nn.ReLU())
        self.actor_mu = torch.nn.Linear(32, output_shape)
        self.actor_sigma = torch.nn.Linear(32, output_shape)

    def forward(self, x):
        x = x.to(device)
        x = self.layer1(x)
        mu = self.actor_mu(x)
        sigma = self.actor_sigma(x)
        return mu, sigma


class ShallowCritic(torch.nn.Module):
    def __init__(self, input_shape, output_shape):
        super(ShallowCritic, self).__init__()
        self.layer1 = torch.nn.Sequential(torch.nn.Linear(input_shape[0], 32),
                                          torch.nn.ReLU())
        self.actor = torch.nn.Linear(32, output_shape)
    def forward(self, x):
        x = x.to(device)
        x = self.layer1(x)
        critic = self.actor(x)
        return critic


class ShallowActorCritic(torch.nn.Module):
    def __init__(self, input_shape, actor_shape, critic_shape, params=None):
        super(ShallowActorCritic, self).__init__()
        self.layer1 = torch.nn.Sequential(torch.nn.Linear(input_shape[0], 32),
                                          torch.nn.ReLU())
        self.layer2 = torch.nn.Sequential(torch.nn.Linear(32, 16),
                                          torch.nn.ReLU())
        self.actor_mu = torch.nn.Linear(16, actor_shape)
        self.actor_sigma = torch.nn.Linear(16, actor_shape)
        self.critic = torch.nn.Linear(16, critic_shape)

    def forward(self, x):
        x.requires_grad_()
        x = x.to(device)
        x = self.layer1(x)
        x = self.layer2(x)
        actor_mu = self.actor_mu(x)
        actor_sigma = self.actor_sigma(x)
        critic = self.critic(x)
        return actor_mu, actor_sigma, critic

class DeepActorCritic(torch.nn.Module):
    def __init__(self, input_shape, actor_shape, critic_shape, params=None):
        """
        Deep convolutional Neural Network to represent both policy  (Actor) and a value function (Critic).
        The Policy is parametrized using a Gaussian distribution with mean mu and variance sigma
        The Actor's policy parameters (mu, sigma) and the Critic's Value (value) are output by the deep CNN implemented
        in this class.
        :param input_shape:
        :param actor_shape:
        :param critic_shape:
        :param params:
        """
        super(DeepActorCritic, self).__init__()
        self.layer1 = torch.nn.Sequential(torch.nn.Conv2d(input_shape[2], 128, 3, stride=1, padding=0),
                                          torch.nn.ReLU())
        self.layer2 = torch.nn.Sequential(torch.nn.Conv2d(128, 64, 3, stride=1, padding=0),
                                          torch.nn.ReLU())
        self.layer3 = torch.nn.Sequential(torch.nn.Conv2d(64, 32, 3, stride=1, padding=0),
                                          torch.nn.ReLU())
        self.layer4 = torch.nn.Sequential(torch.nn.Linear(32 * 78 * 78, 2048),
                                          torch.nn.ReLU())
        self.actor_mu = torch.nn.Linear(2048, actor_shape)
        self.actor_sigma = torch.nn.Linear(2048, actor_shape)
        self.critic = torch.nn.Linear(2048, critic_shape)

    def forward(self, x):
        x.requires_grad_()
        x = x.to(device)
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = x.view(x.shape[0], -1)
        x = self.layer4(x)
        actor_mu = self.actor_mu(x)
        actor_sigma = self.actor_sigma(x)
        critic = self.critic(x)
        return actor_mu, actor_sigma, critic


class DeepActorCriticAgent(object):
    def __init__(self, env_name, state_shape, action_shape, agent_params):
        """
        An Actor-Critic Agent that uses a Deep Neural Network to represent it's Policy and the Value function
        :param state_shape:
        :param action_shape:
        """
        self.env = gym.make(env_name)
        self.state_shape = state_shape
        self.action_shape = action_shape
        self.params = agent_params
        if len(self.state_shape) == 3:  # Screen image is the input to the agent
            self.actor_critic = DeepActorCritic(self.state_shape, self.action_shape, 1, self.params).to(device)
        else:  # Input is a (single dimensional) vector
            #self.actor_critic = ShallowActorCritic(self.state_shape, self.action_shape, 1, self.params).to(device)
            self.actor = ShallowActor(self.state_shape, self.action_shape).to(device)
            self.critic = ShallowCritic(self.state_shape, self.action_shape).to(device)
        self.policy = self.multi_variate_gaussian_policy
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=1e-3)
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=1e-3)
        self.gamma = self.params['gamma']
        self.trajectory = []  # Contains the trajectory of the agent as a sequence of Transitions
        self.rewards = []  #  Contains the rewards obtained from the env at every step
        self.global_step_num = 0

    def multi_variate_gaussian_policy(self, obs):
        """
        Calculates a multi-variate gaussian distribution over actions given observations
        :param obs: Agent's observation
        :return: policy, a distribution over actions for the given observation
        """
        mu, sigma = self.actor(obs)
        value = self.critic(obs)
        [ mu[:, i].clamp_(float(self.env.action_space.low[i]), float(self.env.action_space.high[i]))
        for i in range(self.action_shape)]  # Clamp each dim of mu based on the (low,high) limits of that action dim
        sigma = torch.nn.Softplus()(sigma).squeeze() + 1e-7  # Let sigma be (smoothly) +ve
        self.mu = mu.to(torch.device("cpu"))
        self.sigma = sigma.to(torch.device("cpu"))
        self.value = value.to(torch.device("cpu"))
        if len(self.mu.shape) == 0: # See if mu is a scalar
            #self.mu = self.mu.unsqueeze(0)  # This prevents MultivariateNormal from crashing with SIGFPE
            self.mu.unsqueeze_(0)
        self.action_distribution = MultivariateNormal(self.mu, torch.eye(self.action_shape) * self.sigma, validate_args=True)
        return(self.action_distribution)

    def preproc_obs(self, obs):
        if len(obs.shape) == 3:
            #  Make sure the obs are in this order: C x W x H and add a batch dimension
            obs = np.reshape(obs, (obs.shape[2], obs.shape[1], obs.shape[0]))
            obs = np.resize(obs, (3, 84, 84))
        #  Convert to torch Tensor, add a batch dimension, convert to float repr
        obs = torch.from_numpy(obs).unsqueeze(0).float()
        return obs

    def process_action(self, action):
        [action[:, i].clamp_(float(self.env.action_space.low[i]), float(self.env.action_space.high[i]))
         for i in range(self.action_shape)]  # Limit the action to lie between the (low, high) limits of the env
        action = action.to(torch.device("cpu"))
        return action.numpy().squeeze(0)  # Convert to numpy ndarray, squeeze and remove the batch dimension

    def get_action(self, obs):
        obs = self.preproc_obs(obs)
        action_distribution = self.policy(obs)  # Call to self.policy(obs) also populates self.value with V(obs)
        value = self.value
        action = action_distribution.sample()
        log_prob_a = action_distribution.log_prob(action)
        action = self.process_action(action)
        self.trajectory.append(Transition(obs, value, action, log_prob_a))  # Construct the trajectory
        return action

    def calculate_n_step_return(self, n_step_rewards, final_state, done, gamma):
        """
        Calculates the n-step return for each state in the input-trajectory/n_step_transitions
        :param n_step_rewards: List of rewards for each step
        :param final_state: Final state in this n_step_transition/trajectory
        :param done: True rf the final state is a terminal state if not, False
        :return: The n-step return for each state in the n_step_transitions
        """
        g_t_n_s = list()
        with torch.no_grad():
            g_t_n = torch.tensor([[0]]).float() if done else self.critic(self.preproc_obs(final_state)).cpu()
            for r_t in n_step_rewards[::-1]:  # Reverse order; From r_tpn to r_t
                g_t_n = torch.tensor(r_t) + self.gamma * g_t_n
                g_t_n_s.insert(0, g_t_n)  # n-step returns inserted to the left to maintain correct index order
            return g_t_n_s

    def calculate_loss(self, trajectory, td_targets):
        """
        Calculates the critic and actor losses using the td_targets and self.trajectory
        :param td_targets:
        :return:
        """
        n_step_trajectory = Transition(*zip(*trajectory))
        v_s_batch = n_step_trajectory.value_s
        log_prob_a_batch = n_step_trajectory.log_prob_a
        actor_loss, critic_loss = [], []
        for td_target, critic_prediction, log_p_a in zip(td_targets, v_s_batch, log_prob_a_batch):
            td_err = td_target - critic_prediction
            actor_loss.append(- log_p_a * td_err)  # td_err is an unbiased estimated of Advantage
            critic_loss.append(F.smooth_l1_loss(critic_prediction, td_target))
            #critic_loss.append(F.mse_loss(critic_pred, td_target))
        actor_loss = torch.stack(actor_loss).mean()
        critic_loss = torch.stack(critic_loss).mean()

        writer.add_scalar(self.actor_name + "/critic_loss", critic_loss, self.global_step_num)
        writer.add_scalar(self.actor_name + "/actor_loss", actor_loss, self.global_step_num)

        return actor_loss, critic_loss

    def learn_td_ac(self, s_t, a_t, r, s_tp1, done):
        """
        Learn using (1-step) Temporal Difference Actor-Critic policy gradient
        :param s_t: Observation/state at time step t
        :param a_t: Action taken at time step t
        :param r: Reward obtained for taking a_t at time step t
        :param s_tp1: Observation/reward at time step t+1
        :param done: Whether or not the episode ends/completed at time step t
        :return: None. The internal Actor-Critic parameters are updated
        """
        policy_loss = self.policy(self.preproc_obs(s_t)).log_prob(torch.tensor(a_t))
        # The call to self.policy(s_t) will also calculate and store V(s_t) in self.value
        v_st = self.value
        _ = self.policy(self.preproc_obs(s_tp1))  # This call populates V(s_t+1) in self.value
        v_stp1 = self.value
        td_target = torch.tensor(r) + self.gamma * v_stp1
        td_err = td_target - v_st
        loss = - torch.mean(policy_loss + td_err.pow(2))
        writer.add_scalar("main/loss", loss, global_step_num)
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

    def learn(self, n_th_observation, done):
        td_targets = self.calculate_n_step_return(self.rewards, n_th_observation, done, self.gamma)
        actor_loss, critic_loss = self.calculate_loss(self.trajectory, td_targets)

        self.actor_optimizer.zero_grad()
        actor_loss.backward(retain_graph=True)
        self.actor_optimizer.step()

        self.critic_optimizer.zero_grad()
        critic_loss.backward()
        self.critic_optimizer.step()

        #! TODO: Study the effect of accumulating the trajectories (xp Memory) rather than clearing
        self.trajectory.clear()
        self.rewards.clear()

    def run(self, seed, max_episodes, n_step_learning_step_thresh):
        self.seed = seed
        self.actor_name = "actor" + str(seed)
        for episode in range(max_episodes):
            obs = self.env.reset()
            done = False
            ep_reward = 0.0
            step_num = 0
            while not done:
                action = self.get_action(obs)
                next_obs, reward, done, _ = self.env.step(action)
                self.rewards.append(reward)
                step_num +=1
                if step_num >= n_step_learning_step_thresh or done:
                    self.learn(next_obs, done)
                    step_num = 0
                obs = next_obs
                ep_reward += reward
                self.global_step_num += 1
                #print(self.actor_name + ":Episode#:", episode, "step#:", step_num, "\t rew=", reward, end="\r")
                writer.add_scalar(self.actor_name + "/reward", reward, self.global_step_num)
            print(self.actor_name+ ":Episode#:", episode, "\t ep_reward=", ep_reward)
            writer.add_scalar(self.actor_name + "/ep_reward", ep_reward, self.global_step_num)


if __name__ == "__main__":
    env = gym.make(args.env_name)
    observation_shape = env.observation_space.shape
    action_shape = env.action_space.shape[0]
    agent_params = params_manager.get_agent_params()
    agent = DeepActorCriticAgent(args.env_name, observation_shape, action_shape, agent_params)
    mp.set_start_method('spawn')

    if agent_params["learner"] == "n_step_TD_AC":
       actor_procs = [mp.Process(target=agent.run,
                       args=(i, agent_params["max_num_episodes"], agent_params["learning_step_thresh"]))
                      for i in range(agent_params["num_actors"])]
       [p.start() for p in actor_procs]


    elif agent_params["learner"] == "1_step_TD_AC":
        for episode in range(agent_params["max_num_episodes"]):
            obs = env.reset()
            done = False
            ep_reward = 0
            step_num = 0
            while not done:
                action = agent.get_action(obs)
                next_obs, reward, done, info = env.step(action)
                agent.learn_td_ac(obs, action, reward, next_obs, done)
                obs = next_obs
                ep_reward += reward
                step_num += 1
                global_step_num += 1
                #env.render()
                print("Episode#:", episode, "step#:", step_num, "\t rew=", reward, end="\r")
                writer.add_scalar("main/reward", reward, global_step_num)
            print("Episode#:", episode, "\t ep_reward=", ep_reward)
            writer.add_scalar("main/ep_reward", ep_reward, global_step_num)

    [p.join() for p in actor_procs]


