"""Platform-specific CCB implementations.

Shared runtime contracts stay in their existing packages. Concrete platform
adapters live below this namespace so importing Linux/macOS code does not
implicitly import Windows implementations.
"""
