"""
For now this will only do fprop.  that's probably non-trivial enough for now...
"""
from neon.backends.util.math_helper import magic64
from neon.backends.cuda_templates import _ew_types
import pyopencl as cl

def _get_conv_kernel(ctx, options, dtype, filter_size, bsum, operation, filter_bounds_check=False, debug=False):
    """
    Builds the convolution kernel for a specified filter size.

    Arguments:
        dtype (np.dtype): The data type which the kernel will operate on.
        filter_size (int): Total number of elements per filter (R * S)
        bsum (boolean): If set to true, kernel will include code to compute
            batch sum during fprop
        operation (string): Determines which kernel to build. options follow:
            'fprop': Forward propagation of activations.
            'bprop': Backward propagation of error.
            'update': Computes gradients for filter weights based on error and inputs.
        filter_bounds_check (boolean): Checks if filter weight is in bounds when K is
            not a multiple of 32.
        debug (boolean): When set to true, kernels will be compiled with debug symbols.
    """
    assert operation in ["fprop"]
    assert not bsum
    assert operation in ["fprop", "bprop", "update"]
    if operation == "fprop" or operation == "update":
        lut_code = r"""
    if(tid < 32)
    {
        int rs = tid;
        int base_x, base_y;

        base_x = output_pixel_x * stride_w - padding_w;
        base_y = output_pixel_y * stride_h - padding_h;

        // This will have 1s for this tid, and all the tids below it
        // eg:
        //    1 << 4 - 1
        // => 0b1111
        unsigned int mask = (1 << tid) - 1;

        while(rs < FILTER_SIZE)
        {
            int filter_x, filter_y;
            _idiv_magic32(rs, magic_s, shift_s, S, &filter_y, &filter_x);

            int index_x = base_x + filter_x;
            int index_y = base_y + filter_y;

            //Check if the index is valid
            int in_bounds = (index_x >= 0 && index_x < W && index_y >= 0 && index_y < H);
            // from cuda manual:
            // __ballot(predicate) :
            // Evaluate predicate for all active threads of the warp and return an integer whose
            // Nth bit is set if and only if predicat
// TODO:            unsigned int threads_in_bounds = __ballot(in_bounds);

            //Store lookup table entry
            if(in_bounds)
            {
                int2 lut_entry;
                lut_entry.x = ((index_y * W + index_x) * N) >> 2;
                lut_entry.y = (rs * K) >> 2;

                int index = 0;
                // TODO:      int index = lut_size_local + __popc(threads_in_bounds & mask);
                lookup_table[index] = lut_entry;
            }

     // TODO:       lut_size_local += __popc(threads_in_bounds);

            rs += 32;
        }
    }
"""
    bsum_code = ""

    if operation == "fprop":
        a_name = "image"
        b_name = "filter"

    if filter_bounds_check:
        filter_load_cond = "int filter_load_in_bounds = (((filter_id + get_local_id(0)) << 2) < K);"
        check_filter_cond = "(!filter_load_in_bounds) ? make_float4(0, 0, 0, 0) :"
    else:
        filter_load_cond = ""
        check_filter_cond = ""

    header_code = r"""
#define TILE_DIM            32
#define ITEMS_PER_THREAD    4
#define THREADS_DIM         8

#define REG_TILE_X          4
#define REG_TILE_Y          4
#define THREADS_DIM_X       8
#define THREADS_DIM_Y       8
#define SM_TILE_X           (REG_TILE_X * THREADS_DIM_X)
#define SM_TILE_Y           (REG_TILE_Y * THREADS_DIM_Y)

#define NUM_ROWS            8
#define FILTER_SIZE         %(filter_size)s
#define MAGIC_FILTER_SIZE   %(magic_filter_size)s
#define SHIFT_FILTER_SIZE   %(shift_filter_size)s

typedef union Matrix {
    %(type)s4 f4;
    %(type)s f[4];
} Matrix;

static inline void _idiv_fast(int numerator, int denominator, float rcp,
                                 int* p_result, int* p_remainder)
{
    *p_result = (int)((float)numerator * rcp);
    *p_remainder = numerator - (*p_result * denominator);
    *p_result = (*p_remainder >= denominator) ? (*p_result + 1) : *p_result;
    *p_remainder = (*p_remainder >= denominator) ? (*p_remainder - denominator) : *p_remainder;
}

static inline void _idiv_magic(int numerator, unsigned int magic, unsigned int shift,
                                   int denominator, int* p_result, int* p_remainder)
{
    if(magic == 1)
    {
        *p_result = numerator >> shift;
    }
    else
    {
        unsigned long long res64 = numerator * (unsigned long long)magic;
        *p_result = ((int)(res64 >> 32) >> shift);
    }
    *p_remainder = numerator - (*p_result * denominator);
}

static inline void _idiv_magic32(int numerator, unsigned int magic, unsigned int shift,
                                     int denominator, int* p_result, int* p_remainder)
{
    if(magic == 1)
    {
        *p_result = numerator >> shift;
    }
    else
    {
        *p_result = ((numerator * magic) >> shift);
    }
    *p_remainder = numerator - (*p_result * denominator);
}

//Note: N and K must be multiples of 4
//get_group_id(0) is gemm tile id (K dimension) and output pixel id
//get_group_id(1) is gemm tile id (N dimension)
//get_local_id(0) is gemm tile offset (K dimension)
//get_local_id(1) is gemm tile offset (N dimension)
kernel void conv_%(operation)s(
                           %(type)s alpha, %(type)s beta,
                           global Matrix *I,
                           global Matrix *F,
                           global Matrix *O,
                           global float* bsum,
                           int C, int D, int H, int W, int N,
                           int T, int R, int S, int K,
                           int M, int P, int Q,
                           int stride_w, int stride_h, int padding_w, int padding_h,
                           int input_channel_size, int filter_channel_size,
                           int output_filter_size,
                           int output_pixels, int grid_p, int grid_q,
                           unsigned int magic_pq, unsigned int shift_pq,
                           unsigned int magic_q, unsigned int shift_q,
                           unsigned int magic_s, unsigned int shift_s)

"""
    code = r"""
{
    local int2 lookup_table[FILTER_SIZE];
    local int lut_size;
    local Matrix %(a_name)s_data[NUM_ROWS][THREADS_DIM_X];
    local Matrix %(b_name)s_data[NUM_ROWS][THREADS_DIM_Y];

    int lut_size_local = 0;

    //TODO: Use square access pattern to image data to increase cache hits
    int output_pixel, image_id;
    _idiv_magic(get_group_id(0), magic_pq, shift_pq, output_pixels, &image_id, &output_pixel);
    image_id = (image_id * get_local_size(0));

    //Zig zag along x axis to increase cache hits
    int temp_x, temp_y;
    _idiv_magic(output_pixel, magic_q, shift_q, Q, &temp_y, &temp_x);
    int output_pixel_x = (temp_y & 1) ? (Q - temp_x - 1) : temp_x;
    int output_pixel_y = temp_y;
    output_pixel = output_pixel_x + (output_pixel_y * Q);

    int filter_id = get_group_id(1) * get_local_size(1);
    // tid is the id within the workgroup, in a flat 1d space
    int tid = get_local_id(0) + get_local_id(1) * get_local_size(0);

    //Offset buffers based on thread id
    I = &(I[image_id  + get_local_id(0)]);
    F = &(F[filter_id + get_local_id(0)]);

    %(filter_load_cond)s

    //Compute lookup table for filter/image data
%(lut_code)s

    if(tid == 0)
    {
        lut_size = lut_size_local;
    }

    barrier(CLK_LOCAL_MEM_FENCE);

    lut_size_local = lut_size;
    Matrix result[REG_TILE_Y];
    output_pixel = (output_pixel * N) >> 2;
    if(lut_size_local > 0)
    {
        //Evaluate gemm with outer product dimensions N, K and inner product CRS
        int CRS = lut_size_local * C;

        //Compute magic numbers for division by lut_size
        float reciprocal = 1.0f / (float)lut_size_local;

        //Initialize shared mem for first block
        int crs = CRS %% NUM_ROWS;
        crs = (crs == 0) ? 8 : crs;

        int c, rs;
        _idiv_fast(CRS - get_local_id(1) - 1, lut_size_local, reciprocal, &c, &rs);

        int2 lut_entry = ((get_local_id(1) & 7) >= crs) ? (int2)0 : lookup_table[rs];
        %(a_name)s_data[get_local_id(1)][get_local_id(0)].f4 =
            ((get_local_id(1) & 7) >= crs) ? (float4)0.0f :
            I[(c * input_channel_size)  + lut_entry.x].f4;
        %(b_name)s_data[get_local_id(1)][get_local_id(0)].f4 = %(check_filter_cond)s
            ((get_local_id(1) & 7) >= crs) ? (float4)0.0f :
            F[(c * filter_channel_size) + lut_entry.y].f4;

        //Iterate over entire filter
        for(crs = CRS - crs - 1; crs > 0; crs -= NUM_ROWS)
        {
            barrier(CLK_LOCAL_MEM_FENCE);

            #pragma unroll
            for(int i = 0; i < NUM_ROWS; i++)
            {
                Matrix load_row;
                Matrix load_col;

                load_row.f4 = %(a_name)s_data[i][get_local_id(0)].f4;
                load_col.f4 = %(b_name)s_data[i][get_local_id(1)].f4;

                //Accumulate product
                #pragma unroll
                for(int q_offset = 0; q_offset < REG_TILE_Y; q_offset++)
                {
                    #pragma unroll
                    for(int p_offset = 0; p_offset < REG_TILE_X; p_offset++)
                    {
                        result[q_offset].f[p_offset] += (load_row.f[p_offset] *
                                                         load_col.f[q_offset]);
                    }
                }
            }

            barrier(CLK_LOCAL_MEM_FENCE);

            //Load new image data and filter weights
            _idiv_fast(crs - get_local_id(1), lut_size_local, reciprocal, &c, &rs);

            lut_entry = lookup_table[rs];
            %(a_name)s_data[get_local_id(1)][get_local_id(0)].f4 =
                I[(c * input_channel_size)  + lut_entry.x].f4;
            %(b_name)s_data[get_local_id(1)][get_local_id(0)].f4 =
                %(check_filter_cond)s F[(c * filter_channel_size) + lut_entry.y].f4;
        }

        barrier(CLK_LOCAL_MEM_FENCE);

        //Accumulate product for last iteration
        #pragma unroll
        for(int i = 0; i < NUM_ROWS; i++)
        {
            Matrix load_row;
            Matrix load_col;

            load_row.f4 = %(a_name)s_data[i][get_local_id(0)].f4;
            load_col.f4 = %(b_name)s_data[i][get_local_id(1)].f4;

            //Accumulate product
            #pragma unroll
            for(int q_offset = 0; q_offset < REG_TILE_Y; q_offset++)
            {
                #pragma unroll
                for(int p_offset = 0; p_offset < REG_TILE_X; p_offset++)
                {
                    result[q_offset].f[p_offset] += (load_row.f[p_offset] * load_col.f[q_offset]);
                }
            }
        }
    }

    //Store result
    filter_id = (filter_id + get_local_id(1)) << 2;
    if(filter_id < K)
    {
        image_id += get_local_id(0);

        #pragma unroll
        for(int q_offset = 0; q_offset < 4; q_offset++)
        {
            if(filter_id < K)
            {
                int out_index = (filter_id * output_filter_size) + output_pixel + image_id;
                %(bsum_code)s

                Matrix cur_value;
                cur_value.f4 = (float4)0.0f;
                if(beta > 0.0f)
                {
                    cur_value.f4 = O[out_index].f4;
                }

                result[q_offset].f[0] = (result[q_offset].f[0] * alpha) + (cur_value.f[0] * beta);
                result[q_offset].f[1] = (result[q_offset].f[1] * alpha) + (cur_value.f[1] * beta);
                result[q_offset].f[2] = (result[q_offset].f[2] * alpha) + (cur_value.f[2] * beta);
                result[q_offset].f[3] = (result[q_offset].f[3] * alpha) + (cur_value.f[3] * beta);

                O[out_index].f4 = result[q_offset].f4;
            }
            filter_id++;
        }
    }
}
"""

    code = header_code + code

    magic = magic64(filter_size)

    code = code % {
        "filter_size":          filter_size,
        "magic_filter_size":    magic[0],
        "shift_filter_size":    magic[1],
        "type":                 _ew_types[dtype]["type"],
        "lut_code":             lut_code,
        "bsum_code":            bsum_code,
        "operation":            operation,
        "a_name":               a_name,
        "b_name":               b_name,
        "filter_load_cond":     filter_load_cond,
        "check_filter_cond":    check_filter_cond
    }
    with open('/tmp/out.cl', 'w') as f:
        f.write(code)

#    options = ["--use_fast_math"]
#    if debug and operation == "bprop":
#        options = options + ["-g", "-G"]
    module = cl.Program(ctx, code).build()

#    kernel = module.get_function("conv_" + operation)
#    kernel.prepare("ffPPPPIIIIIIIIIIIIIIIIIIIIIIIIIIII")
#    kernel.name = "conv_" + operation
#    return kernel

