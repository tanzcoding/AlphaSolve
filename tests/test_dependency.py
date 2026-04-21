import json

dependencies = "[1, 3, 4]"

deps = json.loads(dependencies)

print(type(deps))
print(deps)  # Output: [1, 3, 4]
print(type(deps[0]))  # Output: 1