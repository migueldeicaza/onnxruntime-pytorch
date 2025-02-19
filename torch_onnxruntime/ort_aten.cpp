// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

#include "ort_aten.h"
#include "ort_tensor.h"

namespace torch_ort {
namespace eager {

#pragma region Helpers

namespace {
  inline bool is_device_supported(at::DeviceType type) {
    return type == at::kORT || type == at::kCPU;
  }

  inline void assert_tensor_supported(const at::Tensor& tensor) {
    if (tensor.is_sparse()) {
      throw std::runtime_error("ORT copy: sparse not supported");
    }

    if (tensor.is_quantized()) {
      throw std::runtime_error("ORT copy: quantized not supported");
    }

    if (!is_device_supported(tensor.device().type())) {
      throw std::runtime_error("ORT copy: device not supported");
    }
  }
}

const at::Tensor aten_tensor_from_ort(
  OrtValue&& ot,
  const at::TensorOptions& options) {
  return at::Tensor(c10::make_intrusive<ORTTensorImpl>(
    std::move(ot),
    options));
}

const onnxruntime::MLDataType ort_scalar_type_from_aten(
  at::ScalarType dtype) {
  switch (dtype){
    case at::kFloat:
      return onnxruntime::DataTypeImpl::GetType<float>();
    case at::kDouble:
      return onnxruntime::DataTypeImpl::GetType<double>();
    case at::kHalf:
      return onnxruntime::DataTypeImpl::GetType<onnxruntime::MLFloat16>();
    case at::kBFloat16:
      return onnxruntime::DataTypeImpl::GetType<onnxruntime::BFloat16>();
    case at::kInt:
      return onnxruntime::DataTypeImpl::GetType<int>();
    case at::kShort:
      return onnxruntime::DataTypeImpl::GetType<int16_t>();
    case at::kLong:
      return onnxruntime::DataTypeImpl::GetType<int64_t>();
    default:
      ORT_THROW("Unsupport aten scalar type: ", dtype);
  }
}

const OrtValue create_ort_value(
  onnxruntime::ORTInvoker& invoker,
  const at::Scalar& scalar) {
  // TODO: support more types
  float val = scalar.toFloat();
  OrtValue ort_val;
  CreateMLValue(
    invoker.GetCurrentExecutionProvider().GetAllocator(0, OrtMemTypeDefault),
    ort_scalar_type_from_aten(at::kFloat),
    {},
    &ort_val);
  // TODO: use EP's data transfer to copy the data into that tensor
  auto* ort_tensor = ort_val.GetMutable<onnxruntime::Tensor>();
  CopyVectorToTensor<float>({val}, *ort_tensor);
  return ort_val;
}

const OrtValue create_ort_value(
  onnxruntime::ORTInvoker& invoker,
  const at::Tensor& tensor) {
  assert_tensor_supported(tensor);

  auto* impl = dynamic_cast<ORTTensorImpl*>(tensor.unsafeGetTensorImpl());
  if (impl) {
    return impl->tensor();
  }

  OrtValue ort_tensor;
  CreateMLValue(
    tensor.data_ptr(),
    ort_scalar_type_from_aten(tensor.scalar_type()),
    tensor.sizes().vec(),
    &ort_tensor);
  return ort_tensor;
}

const onnx::AttributeProto create_ort_attribute(
  const char* name,
  at::Scalar value) {
  onnx::AttributeProto attr;
  attr.set_name(name);
  // FIXME: we may need to know the type of the target ORT attribute for
  // conversion. So far, we don't, but there may come a time...
  switch (value.type()) {
    case at::ScalarType::Bool:
      attr.set_type(onnx::AttributeProto_AttributeType::AttributeProto_AttributeType_INT);
      attr.set_i(value.to<int32_t>());
      break;
    case at::ScalarType::Double:
    case at::ScalarType::Long:
      attr.set_type(onnx::AttributeProto_AttributeType::AttributeProto_AttributeType_FLOAT);
      attr.set_f(value.to<double>());
      break;
    // NB. see FIXME above
    // case at::ScalarType::Long:
    //   attr.set_type(onnx::AttributeProto_AttributeType::AttributeProto_AttributeType_INT);
    //   attr.set_f(value.to<int64_t>());
    //   break;
    default:
      // For most at::ScalarType, it should be safe to just call value.to<>
      // on it, but for now we want to explicitly know when we've encountered
      // a new scalar type while bringing up ORT eager mode.
      ORT_THROW("Unsupported: at::ScalarType::", value.type());
  }

  return attr;
}

#pragma endregion

#pragma region Hand-Implemented ATen Ops

at::Tensor ort_op_aten_empty(
  at::IntArrayRef size,
  // *
  const at::TensorOptions& options,
  c10::optional<at::MemoryFormat> memory_format) {
  ORT_LOG_FN(size, options, memory_format);

  // TODO: validate options and memory format
  // TODO: figure out how to get the correct element type.
  OrtValue ot;
  auto& invoker = GetORTInvoker(options.device());
  CreateMLValue(
    invoker.GetCurrentExecutionProvider().GetAllocator(0, OrtMemTypeDefault),
    ort_scalar_type_from_aten(at::kFloat),
    size.vec(),
    &ot);

  return aten_tensor_from_ort(
    std::move(ot),
    options);
}

at::Tensor ort_op_aten_empty_strided(
  at::IntArrayRef size,
  at::IntArrayRef stride,
  // *
  c10::optional<at::ScalarType> dtype_opt,
  c10::optional<at::Layout> layout_opt,
  c10::optional<at::Device> device_opt,
  c10::optional<bool> pin_memory_opt) {
  ORT_LOG_FN(stride, dtype_opt, layout_opt, device_opt, pin_memory_opt);

  // TODO: handle stride
  // TODO: how to handle type conversion
  OrtValue ot;
  assert(device_opt.has_value());
  // TODO: how to support layout
  assert(!layout_opt.has_value());
  at::ScalarType dtype = c10::dtype_or_default(dtype_opt);
  auto& invoker = GetORTInvoker(*device_opt);
  CreateMLValue(
    invoker.GetCurrentExecutionProvider().GetAllocator(0, OrtMemTypeDefault),
    ort_scalar_type_from_aten(dtype),
    size.vec(),
    &ot);
  return aten_tensor_from_ort(
    std::move(ot),
    at::device(*device_opt).dtype(dtype));
}

at::Tensor ort_op_aten_reshape(at::Tensor const& self, at::IntArrayRef shape) {
  ORT_LOG_FN(self, shape);

  auto& invoker = GetORTInvoker(self.device());
  return aten_tensor_from_ort(
    reshape_copy(
      invoker,
      create_ort_value(invoker, self),
      shape.vec()),
    self.options());
}

at::Tensor ort_op_aten_view(const at::Tensor& self, at::IntArrayRef size) {
  ORT_LOG_FN(self, size);

  auto& invoker = GetORTInvoker(self.device());
  return aten_tensor_from_ort(
    reshape_copy(
      invoker,
      create_ort_value(invoker, self),
      at::infer_size(
        size,
        self.numel())),
    self.options());
}

at::Tensor& ort_op_aten_copy_(
  at::Tensor& self,
  const at::Tensor& src,
  bool non_blocking) {
  ORT_LOG_FN(self, src, non_blocking);

  assert_tensor_supported(self);
  assert_tensor_supported(src);

  auto& invoker = GetORTInvoker(self.device().type() == at::kORT
    ? self.device()
    : src.device());
  const auto ort_src = create_ort_value(invoker, src);
  auto ort_self = create_ort_value(invoker, self);

  copy(invoker, ort_src, ort_self);

  return self;
}

at::Tensor ort_op_aten_zeros_like(
  const at::Tensor& self,
  // *,
  c10::optional<at::ScalarType> dtype,
  c10::optional<at::Layout> layout,
  c10::optional<at::Device> device,
  c10::optional<bool> pin_memory,
  c10::optional<at::MemoryFormat> memory_format){

  auto& invoker = GetORTInvoker(self.device());

  auto ort_in_self = create_ort_value(invoker, self);
  auto& ort_in_self_tensor = ort_in_self.Get<onnxruntime::Tensor>();
  auto& shape = ort_in_self_tensor.Shape();

  OrtValue output;
  //todo: avoid the copy on this small shape vector;
  auto element_type = ort_in_self_tensor.DataType();
  CreateMLValue(invoker.GetCurrentExecutionProvider().GetAllocator(0, OrtMemTypeDefault),
                element_type, shape.GetDims(), &output);
  auto* output_tensor = output.GetMutable<onnxruntime::Tensor>();
  memset(output_tensor->MutableDataRaw(element_type), 0, element_type->Size() * shape.Size());
  return aten_tensor_from_ort(
    std::move(output),
    self.options());
}

at::Tensor& ort_op_aten_zero_(at::Tensor& self){
  auto& invoker = GetORTInvoker(self.device());
  auto ort_in_self = create_ort_value(invoker, self);
  OrtValue flag_val;
  //construct a constant tensor
  auto element_type = onnxruntime::DataTypeImpl::GetType<int64_t>();
  CreateMLValue(invoker.GetCurrentExecutionProvider().GetAllocator(0, OrtMemTypeDefault),
                element_type, {}, &flag_val);
  auto* ort_flag_tensor = flag_val.GetMutable<onnxruntime::Tensor>();
  CopyVectorToTensor<int64_t>({1}, *ort_flag_tensor);

  std::vector<OrtValue> ort_out(1);

  auto status = invoker.Invoke(
    "ZeroGradient", {
      std::move(ort_in_self),
      std::move(flag_val)
    }, ort_out, nullptr, onnxruntime::kMSDomain);

  if (!status.IsOK())
    throw std::runtime_error(
      "ORT return failure status:" + status.ErrorMessage());

  //TODO: fix the inplace
  OrtValue ort_result = ort_out[0];
  auto& ort_result_tensor = ort_result.Get<onnxruntime::Tensor>();
  auto* ort_result_data = ort_result_tensor.DataRaw(ort_result_tensor.DataType());
  auto* ort_self_tensor = ort_in_self.GetMutable<onnxruntime::Tensor>();
  auto* ort_self_data = ort_self_tensor->MutableDataRaw(ort_self_tensor->DataType());
  memcpy(ort_self_data, ort_result_data, ort_self_tensor->DataType()->Size() * ort_self_tensor->Shape().Size());
  return self;
}

#pragma endregion

} // namespace eager
} // namespace torch_ort