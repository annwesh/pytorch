import torch

from cpp_api_parity import torch_nn_modules

'''
`SampleModule` is used by `test_cpp_api_parity.py` to test that Python / C++ API
parity test harness works for `torch.nn.Module` subclasses.

When `SampleModule.has_parity` is true, behavior of `reset_parameters` / `forward` /
`backward` is the same as the C++ equivalent.

When `SampleModule.has_parity` is false, behavior of `reset_parameters` / `forward` /
`backward` is different from the C++ equivalent.
'''

class SampleModule(torch.nn.Module):
    def __init__(self, has_parity, has_submodule):
        super(SampleModule, self).__init__()
        self.has_parity = has_parity
        if has_submodule:
            self.submodule = SampleModule(self.has_parity, False)

        self.has_submodule = has_submodule
        self.register_parameter('param', torch.nn.Parameter(torch.empty(3, 4)))

        self.reset_parameters()

    def reset_parameters(self):
        with torch.no_grad():
            self.param.fill_(1)
            if not self.has_parity:
                self.param.add_(10)

    def forward(self, x):
        submodule_forward_result = self.submodule(x) if hasattr(self, 'submodule') else 0
        if not self.has_parity:
            return x + self.param * 4 + submodule_forward_result + 3
        else:
            return x + self.param * 2 + submodule_forward_result

SAMPLE_MODULE_CPP_SOURCE = """\n
namespace torch {
namespace nn{
struct C10_EXPORT SampleModuleOptions {
  SampleModuleOptions(bool has_parity, bool has_submodule) : has_parity_(has_parity), has_submodule_(has_submodule) {}

  TORCH_ARG(bool, has_parity);
  TORCH_ARG(bool, has_submodule);
};

struct C10_EXPORT SampleModuleImpl : public torch::nn::Cloneable<SampleModuleImpl> {
  explicit SampleModuleImpl(SampleModuleOptions options) : options(std::move(options)) {
    if (options.has_submodule()) {
      submodule = register_module(
        "submodule",
        std::make_shared<SampleModuleImpl>(SampleModuleOptions(options.has_parity(), false)));
    }
    reset();
  }
  void reset() {
    param = register_parameter("param", torch::ones({3, 4}));
  }
  torch::Tensor forward(torch::Tensor x) {
    return x + param * 2 + (submodule ? submodule->forward(x) : torch::zeros_like(x));
  }
  SampleModuleOptions options;
  torch::Tensor param;
  std::shared_ptr<SampleModuleImpl> submodule{nullptr};
};

TORCH_MODULE(SampleModule);
}
}
"""

module_tests = [
    dict(
        module_name='SampleModule',
        desc='has_parity',
        constructor_args=(True, True),
        cpp_constructor_args='torch::nn::SampleModuleOptions(true, true)',
        input_size=(3, 4),
        cpp_input_args=['torch::randn({3, 4})'],
        has_parity=True,
    ),
    dict(
        fullname='SampleModule_no_parity',
        constructor=lambda: SampleModule(False, True),
        cpp_constructor_args='torch::nn::SampleModuleOptions(false, true)',
        input_size=(3, 4),
        cpp_input_args=['torch::randn({3, 4})'],
        has_parity=False,
    ),
]

# yf225 TODO: probably clean this up
torch_nn_modules.module_metadata_map['SampleModule'] = torch_nn_modules.TorchNNModuleMetadata(
    cpp_default_constructor_args='(true)',
    num_attrs_recursive=20,
    cpp_sources=SAMPLE_MODULE_CPP_SOURCE,
    python_ignored_constructor_args=['has_parity'],
    python_ignored_attrs=['has_parity'],
)

torch.nn.SampleModule = SampleModule
