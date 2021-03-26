// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

#include <torch/extension.h>

#include "ort_backends.h"
#include "ort_log.h"
#include "ort_optimizers.h"

namespace torch_ort {
namespace eager {

PYBIND11_MODULE(torch_ort, torch_ort_module) {
  ORT_LOG_DEBUG << "pybind11 module init";

  auto device_module = torch_ort_module.def_submodule("device");
  for (auto const& entry : GetORTBackendsManager().GetBackendKinds()) {
    device_module.def(
      entry.second.c_str(),
      [entry](int device_index) {
        return py::cast<py::object>(
          THPDevice_New(at::Device(at::DeviceType::ORT, device_index)));
      },
      py::arg("device_index") = -1);
  }

  auto optimizer_module = torch_ort_module.def_submodule("optimizers");
  optimizer_module.def("SGD", &optimizers::ort_SGD);
  optimizer_module.def("Adam", &optimizers::ort_Adam);
}

} // namespace eager
} // namespace torch_ort