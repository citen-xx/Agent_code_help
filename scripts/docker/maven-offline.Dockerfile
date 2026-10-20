FROM maven:3.9-eclipse-temurin-17

# Prime the image-local Maven cache while the build environment has network.
# Sandbox containers later run with --network none and only mount /workspace.
WORKDIR /seed
COPY pom.xml /seed/pom.xml
COPY src /seed/src
RUN mvn -B test && rm -rf /seed
