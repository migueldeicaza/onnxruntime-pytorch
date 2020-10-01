from typing import Dict, Set, List, Optional
import yaml

from dataclasses import dataclass

from tools.codegen.selective_build.operator import *

# A SelectiveBuilder holds information extracted from the selective build
# YAML specification.
#
# It includes information about the build's selectivity, the models that
# participated in the selective build, and the set of operators that should
# be included in the build.
#
@dataclass(frozen=True)
class SelectiveBuilder:

    # If true, then the build is not selective, and includes all
    # operators.
    include_all_operators: bool

    # The set of models that participate in this selective build.
    # Used only for debugging in we want to know if a specific model's
    # operators were included in thie build.
    models: Optional[List[PyTorchModelMetadata]]

    # A dictionary of operator -> operator metadata.
    operators: Dict[str, SelectiveBuildOperator]

    @staticmethod
    def get_nop_selector() -> 'SelectiveBuilder':
        return SelectiveBuilder.from_yaml_dict({'include_all_operators': True})

    @staticmethod
    def from_yaml_dict(data: Dict[str, object]) -> 'SelectiveBuilder':
        valid_top_level_keys = {
            'include_all_operators',
            'models',
            'operators',
        }
        top_level_keys = set(data.keys())
        if len(top_level_keys - valid_top_level_keys) > 0:
            raise Exception("Got unexpected top level keys: {}".format(
                ",".join(top_level_keys - valid_top_level_keys),
            ))
        include_all_operators = data.get('include_all_operators', False)
        assert isinstance(include_all_operators, bool)

        models = None
        if 'models' in data:
            models_list = data['models']
            assert isinstance(models_list, list)

            models = list(map(
                lambda x: PyTorchModelMetadata.from_yaml(x),
                models_list,
            ))
        operators = {}
        operators_dict = data.get('operators', {})
        assert isinstance(operators_dict, dict)

        for (k, v) in operators_dict.items():
            operators[k] = SelectiveBuildOperator.from_yaml_dict(k, v)
        return SelectiveBuilder(include_all_operators, models, operators)

    @staticmethod
    def from_yaml_str(config_contents: str) -> 'SelectiveBuilder':
        contents = yaml.load(config_contents)
        return SelectiveBuilder.from_yaml_dict(contents)

    @staticmethod
    def from_yaml_path(config_path: str) -> 'SelectiveBuilder':
        with open(config_path, 'r') as f:
            contents = yaml.load(f)
            return SelectiveBuilder.from_yaml_dict(contents)

    @staticmethod
    def from_legacy_op_registration_allow_list(
            allow_list: Set[str],
            is_root_operator: bool,
            is_used_for_training: bool) -> 'SelectiveBuilder':
        operators = {}
        for op in allow_list:
            operators[op] = {
                'name': op,
                'is_root_operator': is_root_operator,
                'is_used_for_training': is_used_for_training,
                'include_all_overloads': True,
            }
        return SelectiveBuilder.from_yaml_dict({
            'operators': operators,
        })

    def is_operator_selected(self, name: str) -> bool:
        if self.include_all_operators:
            return True

        if name in self.operators:
            return True
        name = strip_operator_overload_name(name)
        return name in self.operators and self.operators[name].include_all_overloads

    def is_operator_selected_for_training(self, name: str) -> bool:
        if not self.is_operator_selected(name):
            return False
        if self.include_all_operators:
            return True

        if name in self.operators:
            op: SelectiveBuildOperator = self.operators[name]
            return op.is_used_for_training
        name = strip_operator_overload_name(name)
        if name not in self.operators:
            return False
        base_op: SelectiveBuildOperator = self.operators[name]
        return base_op.include_all_overloads and base_op.is_used_for_training

    def is_root_operator(self, name: str) -> bool:
        if not self.is_operator_selected(name):
            return False
        if self.include_all_operators:
            return True

        if name in self.operators:
            op: SelectiveBuildOperator = self.operators[name]
            return op.is_root_operator
        name = strip_operator_overload_name(name)
        if name not in self.operators:
            return False
        base_op: SelectiveBuildOperator = self.operators[name]
        return base_op.include_all_overloads and base_op.is_root_operator

    def to_dict(self) -> Dict[str, object]:
        ret = {
            'include_all_operators': self.include_all_operators,
            'operators': {},
        }
        for (op_name, op) in self.operators.items():
            ret['operators'][op_name] = op.to_dict()

        if self.models is not None:
            ret['models'] = list(map(
                lambda m: m.to_dict(),
                self.models,
            ))
        return ret


def combine_selective_builders(lhs: SelectiveBuilder, rhs: SelectiveBuilder) -> SelectiveBuilder:
    include_all_operators = lhs.include_all_operators or rhs.include_all_operators
    models = merge_model_lists(lhs.models, rhs.models)
    operators = merge_operator_dicts(lhs.operators, rhs.operators)
    return SelectiveBuilder(include_all_operators, models, operators)
