
import sys
sys.path.append('../')
import random
from time import time
#import tensorflow as tf
import tensorflow.compat.v1 as tf
from Traffic.A3CNetwork import AC_Network
from Helper import update_target_graph, discount, get_empty_loss_arrays, gae, gae_0, adv
import numpy as np
import matplotlib.pyplot as mpl
from time import sleep
import math



# Worker class
class Worker:
    def __init__(self, game, name, s_size, a_size, number_of_agents, trainer, model_path,
                 global_episodes, amount_of_agents_to_send_message_to,
                 display=False, comm=False, comm_size_per_agent=0, spread_messages=True,
                 comm_delivery_failure_chance=0, comm_gaussian_noise=0, comm_jumble_chance=0):
        self.name = "worker_" + str(name)
        self.is_chief = self.name == 'worker_0'
        print(self.name)

        self.number = name
        self.number_of_agents = number_of_agents
        self.model_path = model_path
        self.trainer = trainer
        self.global_episodes = global_episodes
        self.amount_of_agents_to_send_message_to = amount_of_agents_to_send_message_to

        self.episode_rewards = []
        self.episode_lengths = []
        self.episode_collisions = []
        self.episode_stalls = []
        self.episode_mean_values = []
        with tf.variable_scope(self.name):
            self.increment = self.global_episodes.assign_add(1)
            self.summary_writer = tf.summary.FileWriter("train_" + str(self.number))

        # Create the local copy of the network and the tensorflow op to copy global parameters to local network
        self.local_AC = AC_Network(s_size, a_size, amount_of_agents_to_send_message_to * comm_size_per_agent,
                                   amount_of_agents_to_send_message_to * comm_size_per_agent if spread_messages else comm_size_per_agent,
                                   self.name, trainer)
        self.update_local_ops = update_target_graph('global', self.name)

        # Env Pursuit set-up
        self.env = game
        self.s_size = s_size

        self.comm = comm
        self.display = display
        self.message_size = comm_size_per_agent
        self.spread_messages = spread_messages
        self.spread_rewards = True

        self.comm_delivery_failure_chance = comm_delivery_failure_chance
        self.comm_gaussian_noise = comm_gaussian_noise
        self.comm_jumble_chance = comm_jumble_chance

    def train_weights_and_get_comm_gradients(self, rollout, sess, gamma, ac_network, bootstrap_value=0):
        rollout = np.array(rollout)
        observations = np.stack(rollout[:, 0])
        mess_received = np.stack(rollout[:, 1])  # state t
        actions = rollout[:, 2]
        sent_message = np.vstack(rollout[:, 3])
        rewards = rollout[:, 4]
        # next_observations = rollout[:, 5]
        # next_mess_received = np.stack(rollout[:, 6])  # state t+1
        # terminals = rollout[:, 7]  # whether timestep t was terminal
        values = rollout[:, 8]

        # print("VALUE GRADS")
        # for i in range(len(observations)):
        #    print("\t", observations[i], mess_received[i], "\n\t", sent_message[i])

        # for o, m, a, r in zip(observations, mess_received, actions, rewards):
        #    print(o, m, a, r)
        # print()

        # Here we take the rewards and values from the rollout, and use them to
        # generate the advantage and discounted returns.
        # The advantage function uses "Generalized Advantage Estimation"
        rewards_plus = np.asarray(rewards.tolist() + [bootstrap_value])
        discounted_rewards = discount(rewards_plus, gamma)[:-1]
        value_plus = np.asarray(values.tolist() + [bootstrap_value])
        # GAE (if epsilon=0, its the same as doing regular advantage)
        epsilon = 0
        if epsilon == 0:
            advantages = adv(discounted_rewards, value_plus)
        else:
            advantages = gae(gamma, epsilon, rewards, value_plus)
            advantages = discount(advantages, gamma)

        # Update the global network using gradients from loss
        # Generate network statistics to periodically save
        feed_dict = {ac_network.target_v: discounted_rewards,
                     ac_network.inputs: observations,
                     ac_network.inputs_comm: mess_received,
                     ac_network.actions: actions,
                     ac_network.advantages: advantages}

        v_l, p_l, grads_m, e_l, g_n, v_n, _ = sess.run([ac_network.value_loss,
                                                        ac_network.policy_loss,
                                                        ac_network.gradients_q_message,
                                                        ac_network.entropy,
                                                        ac_network.grad_norms,
                                                        ac_network.var_norms,
                                                        ac_network.apply_grads
                                                        ],
                                                       feed_dict=feed_dict)
        # print("VALUE LOSS",v_l)
        # print("MESSAGE GRADS\n",grads_m)

        # so we want apply gradients for [0,N-1] states, and we use the gradients of messages from [1, N]
        return observations[:-1], mess_received[:-1], sent_message[:-1], grads_m[0][1:], \
               v_l / len(rollout), p_l / len(rollout), e_l / len(rollout), g_n, v_n

    def apply_comm_gradients(self, observations, mess_received, message_sent, message_loss, sess, ac_network):
        target_message = message_sent - message_loss

        # print("GRADIENTS")
        # for i in range(len(message_loss)):
        #    print("\t", observations[i], mess_received[i], "\n\t", message_sent[i],"->",target_message[i])

        # run error on network
        feed_dict = {ac_network.target_message: target_message,
                     ac_network.inputs: observations,
                     ac_network.inputs_comm: mess_received}
        v_l_m, _ = sess.run([ac_network.loss_m, ac_network.apply_grads_m], feed_dict=feed_dict)

        return v_l_m

    # converts a batch of loss of input message into loss of output messages
    # if spread_messages
    #   then agent0 has output [mess0_1, mess0_2], and agent1 and agent2 have inputs mess0_1 and mess0_2
    #   the loss of input mess0_1 and mess0_2 of agent1 and agent2 is copied into loss of agent0's output [mess0_1, mess0_2]
    # else if not spread_messages
    #   then agent0 has output mess0, and agent1 and agent2 have inputs mess0_1 and mess0_2
    #   the loss of input mess0_1 and mess0_2 of agent1 and agent2 is summed into loss of agent0's output mess0
    def input_mloss_to_output_mloss(self, batch_size, mgrad_per_received, comm_map):
        # comm_map is a mapping for each agent i of who sent him messages
        # here, we take the gradients of messages received by i and put them on the messages sent by others

        # print(batch_size, len(states[0]))
        if not self.spread_messages:
            mgrad_per_sent = [[[0 for _ in range(self.message_size)] for _ in range(batch_size)] for _ in
                              range(self.number_of_agents)]
            mgrad_per_sent_mean_counter = [[0 for _ in range(batch_size)] for _ in
                                           range(self.number_of_agents)]
            for j in range(self.number_of_agents):
                for t in range(batch_size):
                    # print(j, t, states[j][t])
                    for index, neighbor in enumerate(comm_map[j][t + 1]):
                        if neighbor != -1:
                            # mgrad_per_sent[neighbor][t] += mgrad_per_received[j][t][
                            #                               index * self.message_size:index * self.message_size + self.message_size]
                            for m in range(self.message_size):
                                mgrad_per_sent[neighbor][t][m] = (mgrad_per_sent_mean_counter[neighbor][t] *
                                                                  mgrad_per_sent[neighbor][t][m] +
                                                                  mgrad_per_received[j][t][
                                                                      index * self.message_size + m]) / (
                                                                     mgrad_per_sent_mean_counter[neighbor][t] + 1)
                            mgrad_per_sent_mean_counter[neighbor][t] += 1
                            # TODO could this be a mean instead of a sum
        else:
            mgrad_per_sent = [[[] for _ in range(batch_size)] for _ in range(self.number_of_agents)]
            for j in range(self.number_of_agents):
                for t in range(batch_size):
                    for index, neighbor in enumerate(comm_map[j][t + 1]):
                        if neighbor != -1:
                            # TODO this is only correct if the spread messages are sent for agents in order of their indexes
                            mgrad_per_sent[neighbor][t].extend(mgrad_per_received[j][t][
                                                               index * self.message_size:index * self.message_size + self.message_size])
        # print("receiving loss\n", mgrad_per_received)
        # print("with comm map\n", comm_map)
        # print("equals sending loss\n", mgrad_per_sent)

        return mgrad_per_sent

    # converts sent messages into received messages
    # if spread_messages
    #   then agent0 has output [mess0_1, mess0_2], and agent1 and agent2 have inputs mess0_1 and mess0_2
    #   the sent messages mess0_1 and mess0_2 are copied into input of agent1 and agent2
    # else if not spread_messages
    #   then agent0 has output mess0, and agent1 and agent2 have inputs mess0_1 and mess0_2
    #   the sent message mess0 is copied into input of agent1 and agent2
    def output_mess_to_input_mess(self, message, comm_map):
        curr_comm = []
        no_mess = np.ones(self.message_size) * 0

        if self.spread_messages:
            # for agent_state in states:
            for j, agent_state in enumerate(comm_map):
                curr_agent_comm = []
                for neighbor in agent_state:
                    if neighbor != -1:
                        # print("message from ", neighbor, "to", j)
                        # TODO this is incorrect, it will copy entire message for all agents and not the specific one
                        curr_agent_comm.extend(message[neighbor])
                    else:
                        curr_agent_comm.extend(no_mess)
                curr_comm.append(curr_agent_comm)
        else:
            # for agent_state in states:
            for j, agent_state in enumerate(comm_map):
                curr_agent_comm = []
                # print(agent_state)
                for neighbor in agent_state:
                    if neighbor != -1:
                        # print("message from ", neighbor, "to", j)
                        curr_agent_comm.extend(message[neighbor])
                    else:
                        curr_agent_comm.extend(no_mess)
                curr_comm.append(curr_agent_comm)

        # print("sending\n", message)
        # print("with comm map\n", comm_map)
        # print("equals receiving\n", curr_comm)

        return curr_comm

    def work(self, max_episode_length, gamma, sess, coord=None, saver=None, max_episodes=None, batch_size=25):
        episode_count = sess.run(self.global_episodes)
        total_steps = 0
        print("Starting worker " + str(self.number))
        # with sess.as_default(), sess.graph.as_default():
        prev_clock = time()
        if coord is None:
            coord = sess

        #comms_evol = [[], [], [], []]
        #if self.display and self.is_chief:
        #    mpl.ion()
        #    mpl.pause(0.001)

        while not coord.should_stop():
            sess.run(self.update_local_ops)

            episode_buffer = [[] for _ in range(self.number_of_agents)]
            episode_comm_maps = [[] for _ in range(self.number_of_agents)]
            episode_values = [[] for _ in range(self.number_of_agents)]
            episode_reward = 0
            episode_step_count = 0
            action_indexes = list(range(self.env.max_actions))

            v_l, p_l, e_l, g_n, v_n = get_empty_loss_arrays(self.number_of_agents)
            partial_obs = [None for _ in range(self.number_of_agents)]
            partial_mess_rec = [None for _ in range(self.number_of_agents)]
            sent_message = [None for _ in range(self.number_of_agents)]
            mgrad_per_received = [None for _ in range(self.number_of_agents)]

            # start new epi
            current_screen, _ = self.env.reset()
            for i in range(self.number_of_agents):
                episode_comm_maps[i].append(current_screen[i][1:4])

                # replace state to just show whether cars were there or not, and not which cars
                for neighbor_index in range(1, 4):
                    if current_screen[i][neighbor_index] >= 0:
                        current_screen[i][neighbor_index] = 1

            if self.is_chief and self.display:
                self.env.render()

            curr_comm = [[] for _ in range(self.number_of_agents)]
            for curr_agent in range(self.number_of_agents):
                for from_agent in range(self.amount_of_agents_to_send_message_to):
                    curr_comm[curr_agent].extend([0] * self.message_size)

            for episode_step_count in range(max_episode_length):
                # feedforward pass
                action_distribution, value, message = sess.run([self.local_AC.policy, self.local_AC.value,
                                                                self.local_AC.message],
                                                               feed_dict={self.local_AC.inputs: current_screen,
                                                                          self.local_AC.inputs_comm: curr_comm})
                actions = [np.random.choice(action_indexes, p=act_distribution)
                           for act_distribution in action_distribution]

                # TODO hardcoded comms
                #if self.message_size == 1:
                #    for i in range(self.number_of_agents):
                #        message[i] = list(current_screen[i][0])

                # message gauss noise
                if self.comm_gaussian_noise != 0:
                    for index in range(len(message)):
                        message[index] += np.random.normal(0, self.comm_gaussian_noise)

                previous_screen = current_screen
                previous_comm = curr_comm

                # print("OUTPUT MESS", message)

                # Watch environment
                current_screen, reward, terminal, info = self.env.step(actions)
                this_turns_comm_map = []
                for i in range(self.number_of_agents):
                    # 50% chance of no comms
                    surviving_comms = current_screen[i][1:4]
                    for index in range(len(surviving_comms)):
                        if random.random() < self.comm_delivery_failure_chance:  # chance of failure comms
                            surviving_comms[index] = -1
                    episode_comm_maps[i].append(surviving_comms)
                    this_turns_comm_map.append(surviving_comms)

                    # replace state to just show whether cars were there or not, and not which cars
                    for neighbor_index in range(1, 4):
                        if current_screen[i][neighbor_index] >= 0:
                            current_screen[i][neighbor_index] = 1
                curr_comm = self.output_mess_to_input_mess(message, this_turns_comm_map)

                # jumbles comms
                if self.comm_jumble_chance != 0:
                    for i in range(self.number_of_agents):
                        joint_comm = [0] * self.message_size
                        for index in range(len(curr_comm[i])):
                            joint_comm[index % self.message_size] += curr_comm[i][index]
                        jumble = False
                        for index in range(len(curr_comm[i])):
                            if index % self.message_size == 0:
                                # only jumble messages that got received
                                jumble = curr_comm[i][index] != 0 and random.random() < self.comm_jumble_chance
                            if jumble:
                                curr_comm[i][index] = joint_comm[index % self.message_size]

                if self.is_chief and self.display:
                    self.env.render()
                    sleep(0.2)

                episode_reward += sum(reward) if self.spread_rewards else reward

                for i in range(self.number_of_agents):
                    episode_buffer[i].append([previous_screen[i],
                                              previous_comm[i], actions[i], message[i],
                                              reward[i] if self.spread_rewards else reward,
                                              current_screen[i], curr_comm[i], terminal, value[i]])
                    episode_values[i].append(np.max(value[i]))

                # If the episode hasn't ended, but the experience buffer is full, then we make an update step
                # using that experience rollout.
                if len(episode_buffer[0]) == batch_size and not terminal and \
                                episode_step_count < max_episode_length - 1:
                    v1 = sess.run(self.local_AC.value, feed_dict={self.local_AC.inputs: current_screen,
                                                                  self.local_AC.inputs_comm: curr_comm})
                    for i in range(self.number_of_agents):
                        partial_obs[i], partial_mess_rec[i], sent_message[i], mgrad_per_received[i], \
                        v_l[i], p_l[i], e_l[i], g_n[i], v_n[i] = \
                            self.train_weights_and_get_comm_gradients(episode_buffer[i], sess, gamma, self.local_AC,
                                                                      bootstrap_value=v1[i][0])

                    if self.comm:
                        mgrad_per_sent = self.input_mloss_to_output_mloss(batch_size - 1, mgrad_per_received,
                                                                          episode_comm_maps)

                        # start a new mini batch with only the last sample in the comm map
                        temp_episode_comm_maps = []
                        for i in range(self.number_of_agents):
                            temp_episode_comm_maps.append([episode_comm_maps[i][-1]])
                        episode_comm_maps = temp_episode_comm_maps
                        for i in range(self.number_of_agents):
                            self.apply_comm_gradients(partial_obs[i], partial_mess_rec[i],
                                                      sent_message[i], mgrad_per_sent[i], sess, self.local_AC)

                    # print("Copying global networks to local networks")
                    sess.run(self.update_local_ops)

                    # reset episode buffers. keep last value to be used for t_minus_1 message loss
                    temp_episode_buffer = []
                    for i in range(self.number_of_agents):
                        temp_episode_buffer.append([episode_buffer[i][-1]])
                    episode_buffer = temp_episode_buffer
                    # exit()

                # Measure time and increase episode step count
                total_steps += 1
                if total_steps % 2000 == 0:
                    new_clock = time()
                    print(2000.0 / (new_clock - prev_clock), "it/s,   ")
                    prev_clock = new_clock

                # If both prey and predator have acknowledged game is over, then break from episode
                if terminal:
                    break

            # print("0ver ",episode_step_count,episode_reward)
            self.episode_rewards.append(episode_reward)
            self.episode_lengths.append(episode_step_count)
            self.episode_collisions.append(info["collisions"])
            self.episode_stalls.append(info["stalls"])
            self.episode_mean_values.append(np.mean(episode_values))

            # Update the network using the experience buffer at the end of the episode.
            for i in range(self.number_of_agents):
                partial_obs[i], partial_mess_rec[i], sent_message[i], mgrad_per_received[i], \
                v_l[i], p_l[i], e_l[i], g_n[i], v_n[i] = \
                    self.train_weights_and_get_comm_gradients(episode_buffer[i], sess, gamma, self.local_AC)

            if self.comm and len(mgrad_per_received[0]) != 0:
                mgrad_per_sent = self.input_mloss_to_output_mloss(len(mgrad_per_received[0]), mgrad_per_received,
                                                                  episode_comm_maps)

                for i in range(self.number_of_agents):
                    self.apply_comm_gradients(partial_obs[i], partial_mess_rec[i],
                                              sent_message[i], mgrad_per_sent[i], sess, self.local_AC)

            # print("Copying global networks to local networks")
            sess.run(self.update_local_ops)

            # Periodically save gifs of episodes, model parameters, and summary statistics.
            if episode_count % 5 == 0:

                # Save statistics for TensorBoard
                mean_length = np.mean(self.episode_lengths[-5:])
                mean_collisions = np.mean(self.episode_collisions[-5:])
                mean_stalls = np.mean(self.episode_stalls[-5:])
                mean_reward = np.mean(self.episode_rewards[-5:])
                mean_value = np.mean(self.episode_mean_values[-5:])

                """if self.is_chief and self.message_size == 1 and episode_count % 10 == 0:
                    neighbor0 = 0 if random.random() > 0.5 else 1
                    neighbor1 = 0 if random.random() > 0.5 else 1
                    # neighbor2 = 0 if random.random() > 0.5 else 1
                    vals = sess.run(self.local_AC.message,
                                    feed_dict={self.local_AC.inputs: [[-1, neighbor0, neighbor1, 0],
                                                                      [1, neighbor0, neighbor1, 0],
                                                                      [-1, neighbor0, neighbor1, 1],
                                                                      [1, neighbor0, neighbor1, 1]]})
                    comms_evol[0].append(vals[0])
                    comms_evol[1].append(vals[1])
                    comms_evol[2].append(vals[2])
                    comms_evol[3].append(vals[3])
                    if self.display:
                        mpl.clf()
                        mpl.plot(comms_evol[0], color="green")
                        mpl.plot(comms_evol[1], color="red")
                        mpl.plot(comms_evol[2], color="green")
                        mpl.plot(comms_evol[3], color="red")
                        # mpl.show()
                        mpl.pause(0.001)"""
                if self.is_chief and episode_count % 10 == 0:
                    # [vals, flats] = sess.run([self.local_AC.policy,self.local_AC.flattened_inputs_with_comm],
                    #                feed_dict={self.local_AC.inputs: [[-1, -1, -1, 1],[-1, -1, -1, 1],[1, -1, -1, 1],[1, -1, -1, 1]],
                    #                           self.local_AC.inputs_comm: [[0, 0, -1],[0, 0, 1],[0, 0, -1],[0, 0, 1]]})
                    # print(flats, vals)
                    print("length", mean_length, "reward", mean_reward,
                          "collisions", mean_collisions, "stalls", mean_stalls)

                # Save current model
                if self.is_chief and saver is not None and episode_count % 500 == 0:
                    saver.save(sess, self.model_path + '/model-' + str(episode_count) + '.cptk')
                    print("Saved Model")

                summary = tf.Summary()
                #summary.value.add(tag='Perf/Collisions', simple_value=float(mean_collisions))  # avg episode length
                summary.value.add(tag='Perf/Length', simple_value=float(mean_length))  # avg episode length
                summary.value.add(tag='Perf/Reward', simple_value=float(mean_reward))  # avg reward
                summary.value.add(tag='Perf/Value', simple_value=float(mean_value))  # avg episode value_predator
                summary.value.add(tag='Losses/Value Loss', simple_value=float(np.mean(v_l)))  # value_loss
                summary.value.add(tag='Losses/Policy Loss', simple_value=float(np.mean(p_l)))  # policy_loss
                summary.value.add(tag='Losses/Entropy', simple_value=float(np.mean(e_l)))  # entropy
                summary.value.add(tag='Losses/Grad Norm', simple_value=float(np.mean(g_n)))  # grad_norms
                summary.value.add(tag='Losses/Var Norm', simple_value=float(np.mean(v_n)))  # var_norms
                self.summary_writer.add_summary(summary, episode_count)
                self.summary_writer.flush()

            # Update episode count
            if self.is_chief:
                episode_count = sess.run(self.increment)
                if episode_count % 50 == 0:
                    print("Global episodes @", episode_count)

                if max_episodes is not None and episode_count > max_episodes:
                    coord.request_stop()
            else:
                episode_count = sess.run(self.global_episodes)

        self.env.close()

        """if self.is_chief:
            with open("coll_stall.log", "w") as f:
                f.write(str(self.episode_collisions) + "\n")
                f.write(str(self.episode_stalls) + "\n")
                if self.message_size == 1:
                    f.write(str(comms_evol[0]) + "\n")
                    f.write(str(comms_evol[1]) + "\n")
                    f.write(str(comms_evol[2]) + "\n")
                    f.write(str(comms_evol[3]) + "\n")
            pass"""
