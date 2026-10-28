from setuptools import find_packages, setup


setup(
    name="roboguard-hospital-route",
    version="0.1.0",
    description="Standalone RoboGuard hospital navigation route checker",
    packages=find_packages("src"),
    package_dir={"": "src"},
    python_requires=">=3.10",
    install_requires=[
        "jsonschema>=4.0,<5.0",
        "openai>=3.6,<4.0",
        "tiktoken>=0.5",
    ],
    extras_require={"dev": ["pytest>=7.0"]},
)
