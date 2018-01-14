import os, sys
sys.path.append(os.getcwd())

import time
import functools
import re
import numpy as np
import tensorflow as tf
from scipy.misc import imsave

from lib.models import params_with_name
from lib.models.save_images import save_images
from lib.models.distributions import Bernoulli, Gaussian, Categorical, Product
from lib.models.nets_64x64 import NetsRetreiver

TINY = 1e-8
SEED = 123

# CHANGED: ------------------
# 1) Import desired arch for encoder / decoder (CREATE lib.models.nets NetworkRetreiver())
# 2) Contruct latent spec as required...
# 3) All dirs now contained in dirs dict
# 4) self.seed -> SEED
# 5) Save images not longer has to be square -> provide n_rows, n_cols
# 6) checkpoint loading - global step
# 7) data manager
# 8) init_session -> train

# TO DO:
# 1) Loss: Print / tf summaries: total, reconstruct, kl
# 2) Understanding b-vae blend capacity: https://github.com/miyosuda/disentangled_vae/blob/master/model.py

class VAE(object):
    def __init__(self, session, output_dist, z_dist, arch, batch_size, image_shape, exp_name, dirs, 
                 gaps, beta, vis_reconst, vis_disent, n_disentangle_samples):
        """
        :type output_dist: Distribution
        :type z_dist: Gaussian
        """
        self.session = session
        self.output_dist = output_dist
        self.z_dist = z_dist
        self.arch = arch
        self.batch_size = batch_size
        self.image_shape = image_shape
        self.exp_name = exp_name
        self.dirs = dirs
        self.beta = beta
        self.gaps = gaps
        self.vis_reconst = vis_reconst
        self.vis_disent = vis_disent
        self.n_disentangle_samples = n_disentangle_samples
        
        self.__build_graph()

#     def safe_exp(self, x):
#         return tf.exp(tf.clip_by_value(x, 1e-40, 88))

    def __build_graph(self):
        tf.set_random_seed(SEED)
        np.random.seed(SEED)
        
        self.is_training = tf.placeholder(tf.bool)
        self.x = tf.placeholder(tf.int32, shape=[None] + list(self.image_shape))
        
        self.Encoder, self.Decoder = NetsRetreiver(self.arch)        
        norm_x = 2*((tf.cast(self.x, tf.float32)/255.)-.5)
        
        z_dist_params = self.Encoder('Encoder', norm_x, self.image_shape[0], self.z_dist.dist_flat_dim,
                                          self.is_training)
        self.z_dist_info = self.z_dist.activate_dist(z_dist_params)
        self.z = self.z_dist.sample(self.z_dist_info)
        
        x_out_logit = self.Decoder('Decoder', self.z, self.image_shape[0], self.is_training)
        if isinstance(self.output_dist, Gaussian):
            self.x_out = tf.tanh(x_out_logit)
        elif isinstance(self.output_dist, Bernoulli):
            self.x_out = tf.nn.sigmoid(x_out_logit)
        else:
            raise Exception()
        
        self.__prep_loss_optimizer(norm_x, x_out_logit)   
    
    def __prep_loss_optimizer(self, norm_x, x_out_logit):
        norm_x = tf.reshape(norm_x, [-1, self.output_dist.dim])
        
        # reconstruction loss
        if isinstance(self.output_dist, Gaussian):
            reconstr_loss =  tf.reduce_sum(tf.square(norm_x - self.x_out), axis=1)        
        elif isinstance(self.output_dist, Bernoulli):
            reconstr_loss = tf.nn.sigmoid_cross_entropy_with_logits(labels=self.x,
                                                                      logits=x_out_logit)
            reconstr_loss = tf.reduce_sum(reconstr_loss, 1)
        else:
            raise Exception()           
        
        # latent loss
        kl_post_prior = self.z_dist.kl(self.z_dist_info, self.z_dist.prior_dist_info(tf.shape(self.z)[0]))

        # average over batch
        self.loss = tf.reduce_mean(reconstr_loss + self.beta * kl_post_prior)
        self.optimizer = tf.train.AdamOptimizer(learning_rate=1e-4, beta1=0., beta2=0.9).minimize(self.loss) 
    
    def load(self):
        self.saver = tf.train.Saver()
        ckpt = tf.train.get_checkpoint_state(self.dirs['ckpt'])
        
        if ckpt and ckpt.model_checkpoint_path:
            ckpt_name = ckpt.model_checkpoint_path
            self.saver.restore(self.session, ckpt_name)
            print("Checkpoint restored: {0}".format(ckpt_name))
            prev_step = int(next(re.finditer("(\d+)(?!.*\d)",ckpt_name)).group(0))
        
        else:
            print("Failed to find checkpoint.")
            prev_step = 0
        sys.stdout.flush()
        return prev_step + 1
    
    def train(self, n_iters, stats_iters, snapshot_interval):     
        self.session.run(tf.global_variables_initializer())
        
        # Fixed samples
        fixed_x = self.session.run(tf.constant(next(self.train_gen)))
        save_images(fixed_x, os.path.join(self.dirs['samples'], 'samples_groundtruth.png'))
        
        start_iter = self.load()
        running_cost = 0.

        for iteration in range(start_iter, n_iters):
            start_time = time.time()
            
            _data = next(self.train_gen)
            _, cost = self.session.run((self.optimizer, self.loss), feed_dict={self.x: _data, self.is_training:True})
            running_cost += cost
            
            # Print avg stats and dev set stats
            if (iteration < start_iter + 4) or iteration % stats_iters == 0:
                t = time.time()
                dev_data = next(self.dev_gen)
                dev_cost, dev_z_dist_info = self.session.run([self.loss, self.z_dist_info], 
                                                     feed_dict={self.x: dev_data, self.is_training:False})
                
                n_samples = 1. if (iteration < start_iter + 4) else float(stats_iters)
                avg_cost = running_cost / n_samples
                running_cost = 0.                
                print("Iteration:{0} \t| Train cost:{1:.1f} \t| Dev cost: {2:.1f}".format(iteration, avg_cost, dev_cost))
                
                if isinstance(self.z_dist, Gaussian):
                    avg_dev_var = np.mean(dev_z_dist_info["stddev"]**2, axis=0)
                    zss_str = ""
                    for i,zss in enumerate(avg_dev_var):
                       z_str = "z{0}={1:.2f}".format(i,zss)
                       zss_str += z_str + ", "
                    print("z variance:{0}".format(zss_str))
                    #print("z variables ordered by importance:{0}".format(np.argsort(avg_dev_var)))
                
                if self.vis_reconst:
                    self.visualise_reconstruction(fixed_x)
                if self.vis_disent:
                    self.visualise_disentanglement(fixed_x[0])
                
                if np.any(np.isnan(avg_cost)):
                    raise ValueError("NaN detected!")            
            
            if (iteration > start_iter) and iteration % (snapshot_interval) == 0:
                self.saver.save(self.session, os.path.join(self.dirs['ckpt'], self.exp_name), global_step=iteration)  
    
    def encode(self, X, is_training=False):
        """Encode data, i.e. map it into latent space."""
        [z_dist_info] = self.session.run([self.z_dist_info], 
                                         feed_dict={self.x: X, self.is_training: is_training})
        if isinstance(self.z_dist, Gaussian):
            code = z_dist_info["mean"]
        else:
            raise NotImplementedError
        return code
    
    def reconstruct(self, X, is_training=False):
        """ Reconstruct data. """
        return self.session.run(self.x_out, 
                                feed_dict={self.x: X, self.is_training: is_training})
    
    def generate(self, z_mu=None, batch_size=None, is_training=False):
        """ Generate data from code or latent representation."""
        if z_mu is None:
            batch_size = self.batch_size if batch_size is None else batch_size
            z_mu = self.arch.reg_latent_dist.sample_prior(batch_size)
        return self.session.run(self.x_out, feed_dict={self.z: z_mu, self.is_training: is_training})    

    def visualise_reconstruction(self, X):
        X_r = self.reconstruct(X)
        X_r = ((X_r+1.)*(255.99/2)).astype('int32').reshape([-1] + self.image_shape)
        save_images(X_r, os.path.join(self.dirs['samples'], 'samples_reconstructed.png'))
        save_images(X.reshape([-1] + self.image_shape), os.path.join(self.dirs['samples'], 'samples_GT.png'))
    
    def visualise_disentanglement(self, x):
        z = self.encode([x])
        n_zs = z.shape[1] #self.latent_dist.dim
        z = z[0]
        rimgs = []
        
        if isinstance(self.z_dist, Gaussian):
            for target_z_index in range(n_zs):
                for ri in range(self.n_disentangle_samples):
                    value = -3.0 + 6.0 / (self.n_disentangle_samples-1.) * ri
                    z_new = np.zeros((1, n_zs))
                    for i in range(n_zs):
                        if (i == target_z_index):
                            z_new[0][i] = value
                        else:
                            z_new[0][i] = z[i]
                    rimgs.append(self.generate(z_mu=z_new))
        else:
            raise NotImplementedError

        rimgs = np.vstack(rimgs).reshape([-1] + self.image_shape)
        rimgs = ((rimgs+1.)*(255.99/2)).astype('int32')
        save_images(rimgs, os.path.join(self.dirs['samples'], 'disentanglement.png'),
                    n_rows=n_zs, n_cols=self.n_disentangle_samples)