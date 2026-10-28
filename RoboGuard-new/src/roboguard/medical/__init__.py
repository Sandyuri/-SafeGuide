"""Hospital navigation components with dependency-light lazy exports."""

from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    "ALLOW": "guard_types", "PAUSE": "guard_types", "REJECT": "guard_types",
    "ReviewResult": "guard_types",
    "MedicalScene": "scene", "MedicalSceneError": "scene",
    "RouteContext": "task_contracts", "TaskSpec": "task_contracts",
    "DynamicEvent": "joint_planner", "DynamicEventSet": "joint_planner",
    "HospitalRoutePlanner": "joint_planner", "PlanResult": "joint_planner",
    "WorkflowAutomaton": "joint_planner", "route_affected_by_events": "joint_planner",
    "ParetoFrontier": "pareto_types", "ParetoLabel": "pareto_types",
    "ParetoPlanResult": "pareto_types", "ProductState": "pareto_types",
    "insert_nondominated_label": "pareto_types",
    "ParetoRoutePlanner": "pareto_planner", "plan_pareto_candidates": "pareto_planner",
    "CandidateComparison": "route_selector", "RouteSelectionResult": "route_selector",
    "RouteSelector": "route_selector",
    "ChangeClassification": "change_classifier",
    "NavigationChangeClassifier": "change_classifier",
    "DStarLite": "dstar_lite", "DStarRepairResult": "dstar_lite",
    "IncrementalNavigationReplanner": "dstar_lite",
    "IncrementalReplanResult": "dstar_lite",
    "ExecutionPermit": "safety_barrier", "SafetyBarrierResult": "safety_barrier",
    "UnifiedSafetyBarrier": "safety_barrier", "require_execution_permit": "safety_barrier",
    "RouteValidationResult": "route_validation_types",
    "RouteValidationReceipt": "route_validation_types",
    "RouteViolation": "route_validation_types",
    "IndependentRouteValidator": "route_validator",
    "validate_route": "route_validator",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(f"roboguard.medical.{module_name}"), name)
    globals()[name] = value
    return value
