from setuptools import setup, find_packages


# todo add deps, all that
setup(
    name="spine",
    packages=find_packages("src"),
    package_dir={"": "src"},
)
