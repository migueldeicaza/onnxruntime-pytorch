from typing import TextIO, Optional, Dict, List, Tuple

import sys
import json

import opgen.lexer as lexer
import opgen.parser as parser
import opgen.ast as ast
import opgen.writer as writer

class Outputs:
  def __init__(self, count: int):
    self.count = count
    self.name = None

  def __str__(self):
    return self.name if self.name else f'<unbound output>'

class ONNXOpEvalContext:
  ops: ['ONNXOp']

  def __init__(self):
    self.ops = []

  def prepare_outputs(self):
    for i, op in enumerate(self.ops):
      op.outputs.name = f'ort_outputs_{i}_{op.name}'

class ONNXOp:
  def __init__(self,
    name: str,
    outputs: int,
    *inputs: str or Outputs,
    **attributes: Optional[str or Outputs]):
    self.name = name
    self.outputs = Outputs(outputs)
    self.inputs = inputs
    self.attributes = attributes
    self.domain = None

  def eval(self, ctx: ONNXOpEvalContext):
    evaluated_inputs = []

    for i in self.inputs:
      if isinstance(i, ONNXOp):
        i = i.eval(ctx)
      evaluated_inputs.append(i)

    self.inputs = evaluated_inputs

    ctx.ops.append(self)

    return self.outputs

class SignatureOnly(ONNXOp):
  def __init__(self): super().__init__(None, 0)

class FunctionGenerationError(NotImplementedError):
  def __init__(self, cpp_func: ast.FunctionDecl, message: str):
    super().__init__(f'{message}: {cpp_func.torch_func.torch_schema}')

class ORTGen:
  _mapped_ops: Dict[str, ONNXOp] = {}
  function_name_prefix: str

  def __init__(
    self,
    ops: Optional[Dict[str, ONNXOp]] = None,
    function_name_prefix: str = None):
    self._mapped_ops = {}
    self.function_name_prefix = function_name_prefix
    if ops:
      self.register_many(ops)

  def register(self, aten_name: str, onnx_op: ONNXOp):
    self._mapped_ops[aten_name] = onnx_op

  def register_many(self, ops: Dict[str, ONNXOp]):
    for k, v in ops.items():
      self.register(k, v)

  def run(self, cpp_parser: parser.CPPParser, writer: writer.SourceWriter):
    self._write_file_prelude(writer)

    generated_funcs = []
    for cpp_func, torch_func in self._parse_function_decls(cpp_parser):
      if self.function_name_prefix:
        cpp_func.identifier.value = self.function_name_prefix + \
          cpp_func.identifier.value

      torch_op_name = torch_func.identifier.value
      if torch_op_name not in self._mapped_ops:
        continue
      onnx_op = self._mapped_ops[torch_op_name]
      if not onnx_op:
        continue

      signature_only = isinstance(onnx_op, SignatureOnly)

      writer.writeline()
      writer.writeline(f'// {torch_func.torch_schema}')

      self._write_function_signature(writer, cpp_func)
      if signature_only:
        writer.writeline(';')
      else:
        writer.writeline(' {')
        writer.push_indent()
        self._write_function_body(writer, onnx_op, cpp_func)
        writer.pop_indent()
        writer.writeline('}')

      del self._mapped_ops[torch_op_name]

      generated_funcs.append((cpp_func, torch_func))

    self._write_function_registrations(writer, generated_funcs)
    self._write_file_postlude(writer)

    if len(self._mapped_ops) > 0:
      raise Exception('Torch operation(s) could not be parsed for mapping: ' + \
        ', '.join([f'\'{o}\'' for o in self._mapped_ops.keys()]))

  def _write_file_prelude(self, writer: writer.SourceWriter):
    writer.writeline('// AUTO-GENERATED CODE! - DO NOT EDIT!')
    writer.writeline(f'// $ python {" ".join(sys.argv)}')
    writer.writeline()
    writer.writeline('#include <torch/extension.h>')
    writer.writeline()
    writer.writeline('#include <core/providers/dml/OperatorAuthorHelper/Attributes.h>')
    writer.writeline()
    writer.writeline('#include "ort_tensor.h"')
    writer.writeline('#include "ort_aten.h"')
    writer.writeline('#include "ort_log.h"')
    writer.writeline()
    writer.push_namespace('torch_ort')
    writer.push_namespace('eager')
    writer.writeline()
    writer.writeline('using namespace at;')
    writer.writeline('using NodeAttributes = onnxruntime::NodeAttributes;')

  def _write_file_postlude(self, writer: writer.SourceWriter):
    writer.pop_namespaces()

  def _write_function_signature(
    self,
    writer: writer.SourceWriter,
    cpp_func: ast.FunctionDecl):
    cpp_func.return_type.write(writer)
    writer.write(f' {cpp_func.identifier.value}(')
    writer.push_indent()
    for param_list_member in cpp_func.parameters:
      writer.writeline()
      if isinstance(
        param_list_member.member.parameter_type,
        ast.KWArgsSentinelType):
        writer.write('// ')
      param_list_member.write(writer)
    writer.pop_indent()
    writer.write(')')

  def _write_function_body(
    self,
    writer: writer.SourceWriter,
    onnx_op: ONNXOp,
    cpp_func: ast.FunctionDecl):
    assert(len(cpp_func.parameters) > 0)

    return_alias_info = self._get_alias_info(cpp_func.torch_func.return_type)
    if return_alias_info and not return_alias_info.is_writable:
      return_alias_info = None
    in_place_param: ast.ParameterDecl = None

    # Eval the outer ONNX op to produce a topologically ordered list of ops
    ctx = ONNXOpEvalContext()
    onnx_op.eval(ctx)
    ctx.prepare_outputs()

    # Debug Logging
    log_params = ', '.join([p.member.identifier.value for p \
      in cpp_func.parameters if p.member.identifier])
    writer.writeline(f'ORT_LOG_FN({log_params});')
    writer.writeline()

    # Fetch the ORT invoker from an at::Tensor.device()
    # FIXME: find the first at::Tensor param anywhere in the signature
    # instead of simply the first parameter?
    first_torch_param = cpp_func.torch_func.parameters[0].member
    if not isinstance(
      first_torch_param.parameter_type.desugar(),
      ast.TensorType):
      raise FunctionGenerationError(
        cpp_func,
        'First parameter must be an at::Tensor')

    writer.write('auto& invoker = GetORTInvoker(')
    writer.write(first_torch_param.identifier.value)
    writer.writeline('.device());')
    writer.writeline()

    # FIXME: warn if we have not consumed all torch parameters (either as
    # an ORT input or ORT attribute).

    # Perform kernel fission on the ATen op to yield a chain of ORT Invokes
    # e.g. aten::add(x, y, α) -> onnx::Add(x, onnx::Mul(α, y))
    for onnx_op_index, onnx_op in enumerate(ctx.ops):
      # Torch -> ORT inputs
      for op_input in onnx_op.inputs:
        if isinstance(op_input, Outputs):
          continue
        # See if this input is aliased as an in-place tensor
        cpp_param = cpp_func.get_parameter(op_input)
        if return_alias_info and cpp_param and \
          len(cpp_param.torch_param) == 1 and \
          self._get_alias_info(cpp_param.torch_param[0]) == return_alias_info:
          in_place_param = cpp_param

        writer.write(f'auto ort_input_{op_input} = ')
        writer.writeline(f'create_ort_value(invoker, {op_input});')

      # Torch kwargs -> ORT attributes
      attrs = { k:v for k, v in onnx_op.attributes.items() if v }
      if len(attrs) > 0:
        attrs_arg = 'attrs'
        writer.writeline()
        writer.writeline(f'NodeAttributes {attrs_arg}({len(attrs)});')
        for attr_name, attr in attrs.items():
          writer.write(f'{attrs_arg}[AttrName::{attr_name}] = ')
          writer.write('create_ort_attribute(')
          writer.writeline(f'AttrName::{attr_name}, {attr});')
        attrs_arg = f'&{attrs_arg}'
      else:
        attrs_arg = 'nullptr'

      # Outputs vector
      writer.writeline()
      writer.write(f'std::vector<OrtValue> {onnx_op.outputs}')
      writer.writeline(f'({onnx_op.outputs.count});')

      # Perform the invocation
      writer.writeline()
      if onnx_op_index == 0:
        writer.write('auto ')
      writer.writeline(f'status = invoker.Invoke("{onnx_op.name}", {{')
      writer.push_indent()
      for op_input in onnx_op.inputs:
        if isinstance(op_input, Outputs):
          if op_input.count != 1:
            raise FunctionGenerationError(
              cpp_func,
              'multiple outputs not supported')
          op_input = f'{op_input}[0]'
        else:
          op_input = f'ort_input_{op_input}'
        writer.writeline(f'std::move({op_input}),')
      writer.pop_indent()
      writer.write(f'}}, {onnx_op.outputs}, {attrs_arg}')
      if onnx_op.domain:
        writer.write(f', {onnx_op.domain}')
      writer.writeline(');')
      writer.writeline()

      # Assert invocation
      writer.writeline('if (!status.IsOK())')
      writer.push_indent()
      writer.writeline('throw std::runtime_error(')
      writer.push_indent()
      writer.writeline('"ORT return failure status:" + status.ErrorMessage());')
      writer.pop_indent()
      writer.pop_indent()
      writer.writeline()

      # We'll potentially return back to Torch from this op
      return_outputs = onnx_op.outputs

    # TODO: Pick the right "out" Torch parameter; do not assume the first one
    # TODO: Handle mutliple results
    # TODO: Assert return type

    if not return_alias_info:
      writer.writeline('return aten_tensor_from_ort(')
      writer.push_indent()
      writer.writeline(f'std::move({return_outputs}[0]),')
      writer.writeline(f'{first_torch_param.identifier.value}.options());')
      writer.pop_indent()
      return

    if not in_place_param:
      raise Exception(f'"{cpp_func.torch_func.torch_schema}" ' +
        'has alias info on its return type but no associated parameter')

    writer.writeline(f'auto& ort_result_tensor = {return_outputs}[0].Get<onnxruntime::Tensor>();')
    writer.writeline('auto* ort_result_data = ort_result_tensor.DataRaw(ort_result_tensor.DataType());')
    writer.writeline(f'auto* ort_self_tensor = ort_input_{in_place_param.identifier.value}.GetMutable<onnxruntime::Tensor>();')
    writer.writeline('auto* ort_self_data = ort_self_tensor->MutableDataRaw(ort_self_tensor->DataType());')
    writer.writeline('memcpy(ort_self_data, ort_result_data, ort_self_tensor->DataType()->Size() * ort_self_tensor->Shape().Size());')
    writer.writeline(f'return {in_place_param.identifier.value};')

  def _write_function_registrations(
    self,
    writer: writer.SourceWriter,
    generated_funcs: List[Tuple[ast.FunctionDecl, ast.FunctionDecl]]):
    writer.writeline()
    writer.writeline('TORCH_LIBRARY_IMPL(aten, ORT, m) {')
    writer.push_indent()
    writer.writeline('ORT_LOG_DEBUG << "ATen init";')
    for cpp_func, torch_func in generated_funcs:
      reg_function_arg = cpp_func.identifier.value
      if self._torch_function_needs_unboxed_registration(torch_func):
        writer.write('m.impl_UNBOXED(')
      else:
        writer.write('m.impl(')
        reg_function_arg = f'TORCH_FN({reg_function_arg})'
      writer.writeline(f'"{torch_func.identifier.value}", {reg_function_arg});')
    writer.pop_indent()
    writer.writeline('}')
    writer.writeline()

  def _get_alias_info(self, torch_type_or_param: ast.Type or ast.ParameterDecl):
    if isinstance(torch_type_or_param, ast.ParameterDecl):
      torch_type = torch_type_or_param.parameter_type
    else:
      torch_type = torch_type_or_param
    return getattr(torch_type.desugar(), 'alias_info', None)

  def _torch_function_needs_unboxed_registration(
    self,
    torch_func: ast.FunctionDecl):
    if self._torch_type_needs_unboxed_registration(torch_func.return_type):
      return True

    for torch_param in torch_func.parameters:
      if self._torch_type_needs_unboxed_registration(
        torch_param.member.parameter_type):
        return True

    return False

  def _torch_type_needs_unboxed_registration(self, torch_type: ast.Type):
    torch_type = torch_type.desugar()
    if isinstance(torch_type, ast.ArrayType):
      torch_type = torch_type.base_type
    return not isinstance(torch_type, (
      ast.TensorType,
      ast.ScalarType,
      ast.BoolType,
      ast.IntType,
      ast.KWArgsSentinelType))

  def _parse_function_decls(self, cpp_parser: parser.CPPParser):
    # Parse the C++ declarations
    tu = cpp_parser.parse_translation_unit()

    # Parse the Torch schema from the JSON comment that follows each C++ decl
    # and link associated Torch and C++ decls (functions, parameters, returns)
    for cpp_func in tu:
      if cpp_func.semicolon and cpp_func.semicolon.trailing_trivia:
        for trivia in cpp_func.semicolon.trailing_trivia:
          if trivia.kind == lexer.TokenKind.SINGLE_LINE_COMMENT:
            yield self._parse_and_link_torch_function_decl(cpp_func, trivia)
            break

  def _parse_and_link_torch_function_decl(
    self,
    cpp_func: ast.FunctionDecl,
    torch_schema_comment_trivia: lexer.Token):
    metadata = json.loads(torch_schema_comment_trivia.value.lstrip('//'))
    schema = metadata['schema']

    schema_parser = parser.torch_create_from_string(schema)
    schema_parser.set_source_location(cpp_func.semicolon.location)
    torch_func = schema_parser.parse_function()

    torch_func.torch_schema = schema
    torch_func.torch_dispatch = metadata['dispatch'] == 'True'
    torch_func.torch_default = metadata['default'] == 'True'

    cpp_func.torch_func = torch_func

    if cpp_func.return_type:
      cpp_func.return_type.torch_type = torch_func.return_type

    # Synthesize KWArgsSentinelType in the C++ declaration if we have one
    for i, torch_param in enumerate([p.member for p in torch_func.parameters]):
      if isinstance(torch_param.parameter_type, ast.KWArgsSentinelType):
        cpp_func.parameters.members.insert(i, ast.SyntaxListMember(
          torch_param,
          lexer.Token(None, lexer.TokenKind.COMMA, ',')))
        break

    # Link Torch parameters to their C++ counterparts, special casing
    # TensorOptions parameters
    for i, cpp_param in enumerate([p.member for p in cpp_func.parameters]):
      if not getattr(cpp_param, 'torch_param', None):
        cpp_param.torch_param = []

      torch_param_range = 1
      if isinstance(cpp_param.parameter_type.desugar(), ast.TensorOptionsType):
        torch_param_range = 4

      for j in range(torch_param_range):
        torch_param = torch_func.parameters[i + j].member
        cpp_param.torch_param.append(torch_param)

    return cpp_func, torch_func