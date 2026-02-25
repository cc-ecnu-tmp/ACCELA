# TODO: use gradle

JAVAC = javac
JAVA = java
SRC_DIR = src
BUILD_DIR = build
CLASSES_DIR = $(BUILD_DIR)/classes
BIN_DIR = $(BUILD_DIR)/src
TARGET_EXE = $(BIN_DIR)/main

SRCS = $(shell find $(SRC_DIR) -name "*.java")

.PHONY: all clean test compile format

all: $(TARGET_EXE)

format:
	google-java-format -i $(SRCS)
	@echo "Formatted all Java source files."

compile:
	@mkdir -p $(CLASSES_DIR)
	$(JAVAC) -d $(CLASSES_DIR) $(SRCS)

$(TARGET_EXE): compile
	@mkdir -p $(BIN_DIR)
	@echo '#!/bin/bash' > $(TARGET_EXE)
	@echo 'java -cp $(CLASSES_DIR) Main "$$@"' >> $(TARGET_EXE)
	@chmod +x $(TARGET_EXE)
	@echo "Build successful. Executable created at $(TARGET_EXE)"

clean:
	rm -rf $(BUILD_DIR)
	rm -f *.class
	@echo "Cleaned build artifacts."

