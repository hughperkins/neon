# Copyright 2014 Nervana Systems Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Python code to wrap convolution kernels
"""

import numpy as np
import pycuda.driver as drv
from pycuda.compiler import SourceModule
from pycuda.tools import context_dependent_memoize
#from neon.backends import kernel_specs
from neon.backends.cuda_templates import _common_round, _ew_types
from math import ceil
from operator import mul
import sys

if sys.version_info >= (3, 0):
    from functools import reduce


class KernelGroup(object):
    def __init__(self, lib, dtype):

        self.lib = lib
        self.dtype = dtype
        self.dtype_str = dtype.str[1:]
        self.vec_size = 4 if dtype.itemsize == 4 else 8

        if dtype.type is np.float16:
            self.clss = "hconv"
        elif dtype.type is np.float32:
            self.clss = "sconv"
        elif dtype.type is np.int16:
            self.clss = "fconv"
        elif dtype.type is np.int8:
            self.clss = "bconv"
        else:
            raise TypeError("dtype not supported.")

    def __str__(self):
        raise TypeError("please implement __str__ to describe kernel params for logging.")

    def bind_params(self, *args):
        raise TypeError("bind_params not implemented.")

    def execute(self, repeat=1, unbind=True):
        raise TypeError("execute not implemented.")

    def init_bsum(self, bsum, flags):
        flags |= self.flags
        if bsum:
            bsum_gpudata = bsum.gpudata
            self.bsum_zero = [bsum_gpudata, 0, bsum.size, self.lib.stream]
            flags |= 4
        else:
            bsum_gpudata = 0
            self.bsum_zero = 0
            flags &= ~4
        return bsum_gpudata, flags

    def k_partitions(self, K, tiles):
        k = K
        partitions = []
        for tile_K in tiles:
            grid_K = (k + tiles[-1] - 1) // tile_K
            if grid_K > 0:
                partitions.append([tile_K, grid_K, K-k])
                k -= grid_K * tile_K
            if k <= 0:
                break
        return partitions

    def xprop_kernels(self, op, tile_dim, tile_N, grid_N, K, tiles, PQM, RST, args):

        self.kernels = []
        for tile_K, grid_K, offset_K in self.k_partitions(K, tiles):

            kernel_name = "%s_%s_%s%d_N%d" % (self.clss, op, tile_dim, tile_K, tile_N)

            block = (kernel_specs.kernels[kernel_name]["threads"], 1, 1)
            if RST > 1:
                grid = (PQM, grid_K, grid_N)
            else:
                grid = (grid_K, grid_N, PQM)

            params = [
                kernel_name, grid, block, None,
                None, None, None, None, None, None, None, offset_K]
            params.extend(args)

            self.kernels.append(params)


class FpropDirect(KernelGroup):

    def __init__(self, lib, dtype,
                 N, C, K,
                 D, H, W,
                 T, R, S,
                 M, P, Q,
                 pad_d, pad_h, pad_w,
                 str_d, str_h, str_w,
                 relu, bsum):

        super(FpropDirect, self).__init__(lib, dtype)

        assert N % 32 == 0, "N dim must be multiple of 32"
        assert K % self.vec_size == 0, "K dim must be multiple of %d" % self.vec_size

        tile_N = 128 if N > 64 else 64
        grid_N = _ceil_div(N, tile_N)
        tile_K = (128, 64, 32) if tile_N == 128 else (128, 64)

        magic_PQ = _magic64(P*Q)
        magic_Q = _magic64(Q)
        magic_RS = _magic32(R*S*T+32, R*S)
        magic_S = _magic32(R*S+32, S)

        self.xprop_kernels(
            "fprop", "K", tile_N, grid_N, K, tile_K, P*Q*M, R*S*T,
            _flatten([N, K, D, H, W, W*N, H*W*N, D*H*W*N,
                      C, K*R*S*T, R*S*T, R*S, magic_RS, S, magic_S,
                      pad_d, pad_h, pad_w, str_d, str_h, str_w,
                      Q, P*Q, Q*N, P*Q*N, M*P*Q*N, magic_Q, magic_PQ]))

        self.shared = R*S*T * 4 * 2
        self.flags = (relu and 2) + (bsum and 4)

    def bind_params(self, I, F, O, alpha, beta, bsum, flags=0):

        assert I.dtype == F.dtype == O.dtype

        bsum_gpudata, flags = self.init_bsum(bsum, flags)

        for kernel in self.kernels:
            kernel[3:11] = (self.lib.stream, bsum_gpudata, O.gpudata, I.gpudata, F.gpudata,
                            alpha, beta, flags)

    def execute(self, repeat=1, unbind=True):

        for r in range(repeat):

            if self.bsum_zero:
                drv.memset_d32_async(*self.bsum_zero)

            for kernel_params in self.kernels:
                kernel = kernel_specs.get_kernel(kernel_params[0])
                kernel.prepared_async_call(*kernel_params[1:], shared_size=self.shared)

        if unbind:
            self.bsum_zero = None
            for kernel_params in self.kernels:
                kernel_params[3:11] = (None,) * 8

    def __str__(self):
        return "FpropDirect " + str([k[0] for k in self.kernels])


class FpropCuda(KernelGroup):

    def __init__(self, lib, dtype,
                 N, C, K,
                 D, H, W,
                 T, R, S,
                 M, P, Q,
                 pad_d, pad_h, pad_w,
                 str_d, str_h, str_w,
                 bsum):

        super(FpropCuda, self).__init__(lib, dtype)

        assert N % 32 == 0, "N dim must be multiple of 32"
        assert K % self.vec_size == 0, "K dim must be multiple of %d" % self.vec_size

        magic_PQ = _magic64(P*Q)
        magic_Q = _magic64(Q)
        magic_S = _magic32(R*S+32, S)
        HWN = H * W * N
        RST = R * S * T
        KRST = K * RST
        PQ = P * Q
        PQN = PQ * N
        from neon.backends.kernels.cuda.convolution import _get_conv_kernel
        self.kernel = _get_conv_kernel(dtype=self.dtype.str[1:], filter_size=R*S,
                                       bsum=bsum, operation="fprop")
        grid = (PQ * (-(-N // 32)), (-(-K // 32)), 1)
        block = (8, 8, 1)
        static_kernel_args = _flatten([C, D, H, W, N, T, R, S, K, M, P, Q,
                                       str_w, str_h, pad_w, pad_h,
                                       HWN // 4, KRST // 4, PQN // 4,
                                       PQ, 0, 0,
                                       magic_PQ, magic_Q, magic_S])
        self.launch_args = [grid, block] + [None] * 7 + static_kernel_args

        self.shared = RST * 4 * 2
        self.flags = (bsum and 4)

    def bind_params(self, I, F, O, alpha, beta, bsum, flags=0):

        assert I.dtype == F.dtype == O.dtype

        bsum_gpudata, flags = self.init_bsum(bsum, flags)

        self.launch_args[2:9] = (self.lib.stream, alpha, beta,
                                 I.gpudata, F.gpudata, O.gpudata, bsum_gpudata)

    def execute(self, repeat=1, unbind=True):

        for r in range(repeat):

            if self.bsum_zero:
                drv.memset_d32_async(*self.bsum_zero)

            self.kernel.prepared_async_call(*self.launch_args, shared_size=self.shared)

        if unbind:
            self.bsum_zero = None
            self.launch_args[2:9] = (None,) * 7

    def __str__(self):
        return "FpropCuda"


class BpropCuda(KernelGroup):

    def __init__(self, lib, dtype,
                 N, C, K,
                 D, H, W,
                 T, R, S,
                 M, P, Q,
                 pad_d, pad_h, pad_w,
                 str_d, str_h, str_w,
                 bsum):

        super(BpropCuda, self).__init__(lib, dtype)

        assert N % 32 == 0, "N dim must be multiple of 32"
        assert K % self.vec_size == 0, "K dim must be multiple of %d" % self.vec_size

        magic_HW = _magic64(H*W)
        magic_W = _magic64(W)
        magic_RS = _magic32(R*S*T+32, R*S)
        magic_S = _magic32(R*S+32, S)
        HW = H * W
        HWN = HW * N
        RST = R * S * T
        CRST = C * RST
        PQ = P * Q
        PQN = PQ * N

        self.bsum = bsum
        from neon.backends.kernels.cuda.convolution import _get_conv_kernel
        self.kernel = _get_conv_kernel(dtype=self.dtype.str[1:], filter_size=R*S,
                                       bsum=bsum, operation="bprop")
        grid = (HW * (-(-N // 32)), -(-C // 32), 1)
        block = (8, 8, 1)
        static_kernel_args = _flatten([K, M, P, Q, N, T, R, S, C, D, H, W,
                                       str_w, str_h, pad_w, pad_h,
                                       PQN // 4, CRST // 4, HWN // 4,
                                       HW, 0, 0,
                                       magic_HW, magic_W, magic_S])
        self.launch_args = [grid, block] + [None] * 7 + static_kernel_args

        self.shared = R*S*T * 4 * 2
        self.flags = (bsum and 4)

        # generate the kernel args for dim shuffling CTRSK => KTRSC
        shuffle_grid = (_ceil_div(K, 32), _ceil_div(C, 32), R*S*T)
        self.shuffle_size = C*T*R*S*K*dtype.itemsize
        self.shuffle_args = [shuffle_grid, (32, 8, 1), None, None, None]
        self.shuffle_args.extend(_flatten([
            R*S*T*K, R*S*K, S*K, K,
            R*S*T*C, R*S*C, S*C, C,
            R*S, T, R, S, magic_RS, magic_S]))

        lib.set_scratch_size(self.shuffle_size)

    def bind_params(self, I, F, O, alpha, beta, bsum, flags=0):

        assert I.dtype == F.dtype == O.dtype
        if self.bsum:
            assert bsum is not None, "must use initialized bsum config"

        bsum_gpudata, flags = self.init_bsum(bsum, flags)

        filter_temp = self.lib.scratch_buffer(self.shuffle_size)

        self.shuffle_args[2:5] = (self.lib.stream, filter_temp, F.gpudata)
        self.launch_args[2:9] = (self.lib.stream, alpha, beta,
                                 I.gpudata, filter_temp, O.gpudata, bsum_gpudata)

    def execute(self, repeat=1, unbind=True):
        C = self.shuffle_args[12]
        assert C >= 4, "C dim must be 4 or greater for CUDA C backprop kernel"

        shuffle_kernel = _get_shuffle_kernel(self.dtype.str[1:])

        for r in range(repeat):

            if self.bsum_zero:
                drv.memset_d32_async(*self.bsum_zero)

            shuffle_kernel.prepared_async_call(*self.shuffle_args)
            self.kernel.prepared_async_call(*self.launch_args, shared_size=self.shared)

        if unbind:
            self.bsum_zero = None
            self.shuffle_args[2:5] = (None,) * 3
            self.launch_args[2:9] = (None,) * 7

    def __str__(self):
        return "BpropCuda"


class UpdateCuda(KernelGroup):

    def __init__(self, lib, dtype,
                 N, C, K,
                 D, H, W,
                 T, R, S,
                 M, P, Q,
                 pad_d, pad_h, pad_w,
                 str_d, str_h, str_w):

        super(UpdateCuda, self).__init__(lib, dtype)

        assert N % 32 == 0, "N dim must be multiple of 32"

        HWN = H * W * N
        RS = R * S
        RST = RS * T
        KRST = K * RST
        CRSTK = KRST * C
        PQ = P * Q
        PQN = PQ * N
        magic_S = _magic32(R*S+32, S)

        if lib.deterministic:
            grid_P = 1
            grid_Q = 1
            self.determ = CRSTK
        else:
            grid_P = P
            grid_Q = Q
            self.determ = 0

        pq_blocks = grid_P * grid_Q
        magic_PQ = _magic64(pq_blocks)
        magic_Q = _magic64(grid_Q)

        from neon.backends.kernels.cuda.convolution import _get_conv_kernel
        self.kernel = _get_conv_kernel(dtype=self.dtype.str[1:], filter_size=R*S,
                                       bsum=False, operation="update")
        grid = (pq_blocks * (-(-K // 32)), (-(-(C*RS) // 32)), 1)
        block = (8, 32, 1)
        static_kernel_args = _flatten([C, D, H, W, N, T, R, S, K, M, P, Q,
                                       str_w, str_h, pad_w, pad_h,
                                       HWN // 4, KRST // 4, PQN // 4,
                                       PQ, grid_P, grid_Q,
                                       magic_PQ, magic_Q, magic_S])
        self.launch_args = [grid, block] + [None] * 7 + static_kernel_args

        lib.set_scratch_size((self.determ or C*T*R*S*K)*4)

    def update_grid(self, kernel_name, base_blocks, P, Q, SM_count):

        threads = kernel_specs.kernels[kernel_name]["threads"]
        occupancy = kernel_specs.kernels[kernel_name]["occupancy"]

        # warps per scheduler for one block
        occ_per_block = threads / (32.0 * 4.0 * SM_count)

        grid = []
        for p in range(1, P+1):
            for q in range(1, Q+1):

                occup = p*q*base_blocks * occ_per_block
                groups = occup / occupancy
                slots = ceil(groups)

                # This is a heuristic that keeps the balance of work accross the SMs
                # while also maximizing the work that each block does
                heuristic = min(abs(x - slots) for x in range(4, 8)) + (slots - groups) / 100.0

                grid.append((p, q, heuristic))

        grid.sort(key=lambda x: x[-1])

        return (grid[0][0], grid[0][1], threads)

    def bind_params(self, I, E, O, alpha):

        assert I.dtype == E.dtype

        if O.dtype.type is not np.float32:

            update_temp = self.lib.scratch_buffer((self.determ or O.size)*4)

            self.convert_args = [update_temp, "f4", O, False]
        else:
            update_temp = O.gpudata
            self.convert_args = False

        self.zero_args = [update_temp, 0, O.size, self.lib.stream]

        beta = 0.0
        bsum_gpudata = 0
        self.launch_args[2:9] = (self.lib.stream, alpha, beta,
                                 I.gpudata, E.gpudata, O.gpudata, bsum_gpudata)

    def execute(self, repeat=1, unbind=True):

        for r in range(repeat):

            drv.memset_d32_async(*self.zero_args)

            self.kernel.prepared_async_call(*self.launch_args)

            if self.convert_args:
                _fp_convert(*self.convert_args)

        if unbind:
            self.zero_args = self.convert_args = None
            self.launch_args[2:9] = (None,) * 7

    def __str__(self):
        return "UpdateCuda"


class BpropDirect(KernelGroup):

    def __init__(self, lib, dtype,
                 N, C, K,
                 D, H, W,
                 T, R, S,
                 M, P, Q,
                 pad_d, pad_h, pad_w,
                 str_d, str_h, str_w,
                 relu, bsum):

        super(BpropDirect, self).__init__(lib, dtype)

        assert N % 32 == 0, "N dim must be multiple of 32"
        assert C % self.vec_size == 0, "C dim must be multiple of %d" % self.vec_size

        tile_N = 128 if N > 64 else 64
        grid_N = _ceil_div(N, tile_N)
        tile_C = (128, 64, 32) if tile_N == 128 else (128, 64)

        magic_HW = _magic64(H*W)
        magic_W = _magic64(W)
        magic_RS = _magic32(R*S*T+32, R*S)
        magic_S = _magic32(R*S+32, S)
        magic_str_w = _magic32(W + S, str_w)
        magic_str_h = _magic32(H + R, str_h)
        magic_str_d = _magic32(D + T, str_d)

        self.xprop_kernels(
            "bprop", "C", tile_N, grid_N, C, tile_C, D*H*W, R*S*T,
            _flatten([N, C, M, P, Q, Q*N, P*Q*N, M*P*Q*N,
                      K, C*R*S*T, R*S*T, R*S, magic_RS, S, magic_S,
                      pad_d, pad_h, pad_w, str_d, str_h, str_w,
                      W, H*W, W*N, H*W*N, D*H*W*N, magic_W, magic_HW,
                      R, T, magic_str_w, magic_str_h, magic_str_d]))

        self.shared = R*S*T * 4 * 2
        self.flags = (relu and 2) + (bsum and 4)

        # generate the kernel args for dim shuffling CTRSK => KTRSC
        shuffle_grid = (_ceil_div(K, 32), _ceil_div(C, 32), R*S*T)
        self.shuffle_size = C*T*R*S*K*dtype.itemsize
        self.shuffle_args = [shuffle_grid, (32, 8, 1), None, None, None]
        self.shuffle_args.extend(_flatten([
            R*S*T*K, R*S*K, S*K, K,
            R*S*T*C, R*S*C, S*C, C,
            R*S, T, R, S, magic_RS, magic_S]))

        lib.set_scratch_size(self.shuffle_size)

    def bind_params(self, I, F, O, alpha, beta, bsum, flags=0):

        assert I.dtype == F.dtype == O.dtype

        bsum_gpudata, flags = self.init_bsum(bsum, flags)

        filter_temp = self.lib.scratch_buffer(self.shuffle_size)

        self.shuffle_args[2:5] = (self.lib.stream, filter_temp, F.gpudata)

        for kernel in self.kernels:
            kernel[3:11] = (self.lib.stream, bsum_gpudata, O.gpudata, I.gpudata, filter_temp,
                            alpha, beta, flags)

    def execute(self, repeat=1, unbind=True):

        shuffle_kernel = _get_shuffle_kernel(self.dtype_str)

        for r in range(repeat):

            if self.bsum_zero:
                drv.memset_d32_async(*self.bsum_zero)

            shuffle_kernel.prepared_async_call(*self.shuffle_args)

            for kernel_params in self.kernels:
                kernel = kernel_specs.get_kernel(kernel_params[0])
                kernel.prepared_async_call(*kernel_params[1:], shared_size=self.shared)

        if unbind:
            self.bsum_zero = None
            self.shuffle_args[2:5] = (None,) * 3
            for kernel_params in self.kernels:
                kernel_params[3:11] = (None,) * 8

    def __str__(self):
        return "BpropDirect " + str([k[0] for k in self.kernels])


class BpropDirectSmallC(KernelGroup):

    def __init__(self, lib, dtype,
                 N, C, K,
                 D, H, W,
                 T, R, S,
                 M, P, Q,
                 pad_d, pad_h, pad_w,
                 str_d, str_h, str_w):

        super(BpropDirectSmallC, self).__init__(lib, dtype)

        assert N % 32 == 0, "N dim must be multiple of 32"

        magic_PQ = _magic64(P*Q)
        magic_Q = _magic64(Q)
        magic_RST = _magic32(C*R*S*T, R*S*T)
        magic_RS = _magic32(R*S*T+32, R*S)
        magic_S = _magic32(R*S+32, S)

        # special kernel for deconv into first layer
        kernel_name = "%s_bprop_C1_N64" % self.clss

        grid = (P*Q*M, _ceil_div(C*R*S*T, 32), _ceil_div(N, 64))
        block = (32, 1, 1)

        self.kernel = [kernel_name, grid, block, None, None, None, None, None]
        self.kernel.extend(_flatten([
            N, K, D, H, W, W*N, H*W*N, D*H*W*N,
            C, C*R*S*T, R*S*T, magic_RST, R*S, magic_RS, S, magic_S,
            pad_d, pad_h, pad_w, str_d, str_h, str_w,
            Q, P*Q, Q*N, P*Q*N, M*P*Q*N, magic_Q, magic_PQ,
            C*R*S*T*8*dtype.itemsize, M*P*Q*N*8*dtype.itemsize]))

        # generate the kernel args for transpose CRST,K => K,CRST
        shuffle_grid = (_ceil_div(K, 32), _ceil_div(C*R*S*T, 32), 1)
        self.shuffle_size = K*T*R*S*C*dtype.itemsize
        self.shuffle_args = [shuffle_grid, (32, 8, 1), None, None, None, C*R*S*T, K]

        self.zero = C*D*H*W*N * dtype.itemsize

        lib.set_scratch_size(self.shuffle_size)

    def bind_params(self, I, F, O, alpha, beta, bsum, flags=0):

        assert I.dtype == F.dtype == O.dtype

        if beta and beta != 1.0:
            O[:] = O * beta  # pre-apply beta

        self.beta = beta

        self.zero_args = [O.gpudata, 0, self.zero, self.lib.stream]

        filter_temp = self.lib.scratch_buffer(self.shuffle_size)

        self.shuffle_args[2:5] = (self.lib.stream, filter_temp, F.gpudata)

        self.kernel[3:8] = (self.lib.stream, O.gpudata, I.gpudata, filter_temp, alpha)

    def execute(self, repeat=1, unbind=True):

        shuffle_kernel = _get_transpose_kernel(self.dtype_str)

        kernel = kernel_specs.get_kernel(self.kernel[0])
        for r in range(repeat):

            # let atomic adds accumulate on top
            if not self.beta:
                drv.memset_d8_async(*self.zero_args)

            shuffle_kernel.prepared_async_call(*self.shuffle_args)

            kernel.prepared_async_call(*self.kernel[1:])

        if unbind:
            self.zero_args = None
            self.shuffle_args[2:5] = (None,) * 3
            self.kernel[3:8] = (None,) * 5

    def __str__(self):
        return "BpropDirectSmallC " + str(self.kernel[0])


class UpdateDirect(KernelGroup):

    def __init__(self, lib, dtype,
                 N, C, K,
                 D, H, W,
                 T, R, S,
                 M, P, Q,
                 pad_d, pad_h, pad_w,
                 str_d, str_h, str_w):

        super(UpdateDirect, self).__init__(lib, dtype)

        assert N % 32 == 0, "N dim must be multiple of 32"

        magic_RST = _magic32(C*R*S*T, R*S*T)
        magic_RS = _magic32(R*S*T+32, R*S)
        magic_S = _magic32(R*S+32, S)

        grid_C = _ceil_div(C*R*S*T, 128)
        sm_count = _get_sm_count()

        # in float32 for big feature_map layers the smaller tile is actually faster
        # so restrict tile selection to just that.
        if dtype.type is np.float32 and P*Q > 56*56:
            K_tiles = (64,)
        else:
            K_tiles = (128, 64)

        if lib.deterministic:
            determ = "D"
            if K <= 64:
                K_tiles = (64,)
            else:
                K_tiles = K_tiles[0:1]
            self.determ = C*T*R*S*K
        else:
            determ = ""
            self.determ = 0

        self.kernels = []
        for tile_K, grid_K, offset_K in self.k_partitions(K, K_tiles):

            kernel_name = "%s_updat%s_C128_K%d" % (self.clss, determ, tile_K)
            base_blocks = M*grid_C*grid_K

            grid_P, grid_Q, threads = self.update_grid(kernel_name, base_blocks, P, Q, sm_count)
            # print grid_P, grid_Q

            grid_PQ = grid_P * grid_Q
            magic_PQu = _magic64(grid_PQ)
            magic_Qu = _magic64(grid_Q)

            block = (threads, 1, 1)
            if R*S*T > 1:
                grid = (M*grid_PQ, grid_C, grid_K)
            else:
                grid = (grid_C, grid_K, M*grid_PQ)

            self.determ *= M*grid_PQ
            self.determ_shape = (M*grid_PQ, C*T*R*S*K)

            kernel = [kernel_name, grid, block, None, None, None, None, None]
            kernel.extend(_flatten([
                offset_K, N, K, D, H, W, W*N, H*W*N, D*H*W*N,
                C, C*R*S*T, R*S*T, magic_RST, R*S, magic_RS, S, magic_S,
                pad_d, pad_h, pad_w, str_d, str_h, str_w,
                P, Q, P*Q, Q*N, P*Q*N, M*P*Q*N, magic_Qu, magic_PQu,
                grid_P, grid_Q, grid_PQ, C*R*S*T*K]))

            self.kernels.append(kernel)

        lib.set_scratch_size((self.determ or C*T*R*S*K)*4)

    def update_grid(self, kernel_name, base_blocks, P, Q, SM_count):

        threads = kernel_specs.kernels[kernel_name]["threads"]
        occupancy = kernel_specs.kernels[kernel_name]["occupancy"]

        # warps per scheduler for one block
        occ_per_block = threads / (32.0 * 4.0 * SM_count)

        grid = []
        for p in range(1, P+1):
            for q in range(1, Q+1):

                occup = p*q*base_blocks * occ_per_block
                groups = occup / occupancy
                slots = ceil(groups)

                # This is a heuristic that keeps the balance of work accross the SMs
                # while also maximizing the work that each block does
                heuristic = min(abs(x - slots) for x in range(4, 8)) + (slots - groups) / 100.0

                grid.append((p, q, heuristic))

        grid.sort(key=lambda x: x[-1])

        return (grid[0][0], grid[0][1], threads)

    def bind_params(self, I, E, O, alpha):

        assert I.dtype == E.dtype

        if O.dtype.type is not np.float32 or self.determ:

            update_temp = self.lib.scratch_buffer((self.determ or O.size)*4)

            self.convert_args = [update_temp, "f4", O, False]
            if self.determ:
                self.convert_args[3] = self.determ_shape
        else:
            update_temp = O.gpudata
            self.convert_args = False

        self.zero_args = [update_temp, 0, O.size, self.lib.stream]

        for kernel in self.kernels:
            kernel[3:8] = (self.lib.stream, update_temp, I.gpudata, E.gpudata, alpha)

    def execute(self, repeat=1, unbind=True):

        for r in range(repeat):

            if not self.determ:
                drv.memset_d32_async(*self.zero_args)

            for kernel_params in self.kernels:
                kernel = kernel_specs.get_kernel(kernel_params[0])
                kernel.prepared_async_call(*kernel_params[1:])

            if self.convert_args:
                _fp_convert(*self.convert_args)

        if unbind:
            self.zero_args = self.convert_args = None
            for kernel_params in self.kernels:
                kernel_params[3:8] = (None,) * 5

    def __str__(self):
        return "UpdateDirect " + str([k[0] for k in self.kernels])


# Magic numbers and shift amounts for integer division
# Suitable for when nmax*magic fits in 32 bits
# Shamelessly pulled directly from:
# http://www.hackersdelight.org/hdcodetxt/magicgu.py.txt
def _magic32(nmax, d):
    nc = ((nmax + 1) // d) * d - 1
    nbits = len(bin(nmax)) - 2
    for p in range(0, 2 * nbits + 1):
        if 2 ** p > nc * (d - 1 - (2 ** p - 1) % d):
            m = (2 ** p + d - 1 - (2 ** p - 1) % d) // d
            return (m, p)
    raise ValueError("Can't find magic number for division")


# Magic numbers and shift amounts for integer division
# Suitable for when nmax*magic fits in 64 bits and the shift
# lops off the lower 32 bits
def _magic64(d):
    # 3 is a special case that only ends up in the high bits
    # if the nmax is 0xffffffff
    # we can't use 0xffffffff for all cases as some return a 33 bit
    # magic number
    nmax = 0xffffffff if d == 3 else 0x7fffffff
    magic, shift = _magic32(nmax, d)
    if magic != 1:
        shift -= 32
    return (magic, shift)


# flatten a nested list of lists or values
def _flatten(lst):
    return sum(([x] if not isinstance(x, (list, tuple))
                else _flatten(x) for x in lst), [])


def _ceil_div(x, y):
    return -(-x // y)


