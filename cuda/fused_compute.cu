#ifdef FMOE_USE_NCCL

#include <vector>
#include <torch/extension.h>
#include <c10/cuda/CUDAGuard.h>

#include "fused_compute.cuh"

std::vector<torch::Tensor> _fused_forward(
        torch::Tensor input_buf,
        torch::Tensor weight1,
        torch::Tensor weight2,
        torch::Tensor local_expert_count,
        torch::Tensor global_expert_count,
        long global_batch_size,
        long n_workers, bool has_bias) {
    const auto num_expert = local_expert_count.size(0) / n_workers;
    const auto d_hidden = weight1.size(1);
    const auto d_model = weight1.size(2);

    auto smgr = getCudaStreamManager(input_buf.device().index());

    auto global_input_buf = input_buf.new_empty({global_batch_size, d_model});
    auto global_middle_buf = input_buf.new_empty({global_batch_size, d_hidden});
    auto global_output_buf = input_buf.new_empty({global_batch_size, d_model});
    auto output_buf = input_buf.new_empty({input_buf.size(0), d_model});

    AT_DISPATCH_FLOATING_TYPES_AND_HALF(input_buf.scalar_type(), 
            "fmoe_cuda_fused_forward", ([&] {
        fmoe_cuda_fused_forward_impl(
            input_buf.data_ptr<scalar_t>(),
            weight1.data_ptr<scalar_t>(),
            weight2.data_ptr<scalar_t>(),

            global_input_buf.data_ptr<scalar_t>(),
            global_middle_buf.data_ptr<scalar_t>(),
            global_output_buf.data_ptr<scalar_t>(),
            output_buf.data_ptr<scalar_t>(),

            local_expert_count.data_ptr<long>(),
            global_expert_count.data_ptr<long>(),
            d_model, d_hidden, num_expert, n_workers, has_bias,
            smgr);
    }));
    return {output_buf, global_input_buf, global_middle_buf, global_output_buf};
}

#endif

