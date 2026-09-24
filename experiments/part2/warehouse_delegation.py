"""Shared warehouse delegation chain naming and height rules."""


def companion_group(robot, role):
    if not robot.startswith("Robot"):
        raise ValueError(f"Expected Robot group: {robot}")
    return role + robot[len("Robot"):]


def delegation_chain(robot, z):
    if not isinstance(z, (int, float)) or z < 0 or not float(z).is_integer():
        raise ValueError(f"Unsupported item z={z}; expected a nonnegative integer level")
    chain = [robot]
    if z >= 1:
        chain.append(companion_group(robot, "Forklift"))
    if z >= 2:
        chain.append(companion_group(robot, "Drone"))
    return chain


def request_chain(request):
    # Legacy workloads without a height keep their original single-level behavior.
    chain = delegation_chain(request["selected_robot"], request.get("resource_position", {}).get("z", 0))
    if "delegation_chain" in request and request["delegation_chain"] != chain:
        raise ValueError("Workload delegation_chain disagrees with item height")
    return chain
