cmake_minimum_required(VERSION 3.15)
project(DistributedInference CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -pthread")

# Find packages
find_package(Threads REQUIRED)

# ONNX Runtime (CUDA version from pacman)
set(ONNXRUNTIME_INCLUDE_DIR "/usr/include/onnxruntime" CACHE PATH "ONNX Runtime include directory")
set(ONNXRUNTIME_LIB_DIR "/usr/lib" CACHE PATH "ONNX Runtime library directory")

# Find CUDA (optional, for CUDA provider)
find_package(CUDA QUIET)
if(CUDA_FOUND)
    message(STATUS "CUDA found: ${CUDA_VERSION}")
    include_directories(${CUDA_INCLUDE_DIRS})
else()
    message(STATUS "CUDA not found, GPU support may be limited")
endif()

# Include directories
include_directories(
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_SOURCE_DIR}/external/cpp-httplib
    ${CMAKE_SOURCE_DIR}/external/json/include
    ${ONNXRUNTIME_INCLUDE_DIR}
)

# Link directories
link_directories(${ONNXRUNTIME_LIB_DIR})

# Optional tensorforge CompiledEngine backend (ENGINE_BACKEND=compiled).
option(WITH_COMPILED_BACKEND "Link the tensorforge CompiledEngine backend" OFF)
set(TENSORFORGE_DIR "${CMAKE_SOURCE_DIR}/../tensorforge" CACHE PATH "tensorforge repo root")

# Source files
set(COMMON_SOURCES
    src/consistent_hash.cpp
    src/circuit_breaker.cpp
    src/inference_engine.cpp
    src/engine_backend.cpp
)

# Note: batch_processor.h is header-only (template)

# Worker Node executable
add_executable(worker_node
    src/worker_node.cpp
    ${COMMON_SOURCES}
)

target_link_libraries(worker_node
    Threads::Threads
    onnxruntime
)

# Link the prebuilt tensorforge static lib + CUDA runtime into the worker only.
if(WITH_COMPILED_BACKEND)
    if(NOT CUDA_TOOLKIT_ROOT_DIR)
        set(CUDA_TOOLKIT_ROOT_DIR /opt/cuda)
    endif()
    set(CUDA_LIBDIR ${CUDA_TOOLKIT_ROOT_DIR}/lib64)
    message(STATUS "Compiled backend ON: ${TENSORFORGE_DIR} (CUDA ${CUDA_LIBDIR})")
    target_compile_definitions(worker_node PRIVATE WITH_COMPILED_BACKEND)
    target_include_directories(worker_node PRIVATE
        ${TENSORFORGE_DIR}/runtime/include
        ${TENSORFORGE_DIR}/runtime/external)
    target_link_libraries(worker_node
        ${TENSORFORGE_DIR}/runtime/build/libtensorforge_rt.a
        ${CUDA_LIBDIR}/libcublas.so
        ${CUDA_LIBDIR}/libnvrtc.so
        ${CUDA_LIBDIR}/libcudart.so
        ${CUDA_LIBDIR}/stubs/libcuda.so)
endif()

# Gateway executable
add_executable(gateway
    src/gateway.cpp
    ${COMMON_SOURCES}
)

target_link_libraries(gateway
    Threads::Threads
    onnxruntime
)

# Compiler optimizations
if(CMAKE_BUILD_TYPE STREQUAL "Release")
    target_compile_options(worker_node PRIVATE -O3 -march=native)
    target_compile_options(gateway PRIVATE -O3 -march=native)
else()
    target_compile_options(worker_node PRIVATE -Wall -Wextra)
    target_compile_options(gateway PRIVATE -Wall -Wextra)
endif()

# Unit tests for queue timing do not require a running inference service.
include(CTest)
if(BUILD_TESTING)
    add_executable(test_batch_timings tests/test_batch_timings.cpp)
    target_link_libraries(test_batch_timings Threads::Threads)
    add_test(NAME batch_timings COMMAND test_batch_timings)
    set_tests_properties(batch_timings PROPERTIES TIMEOUT 10)

    add_executable(test_batch_shapes tests/test_batch_shapes.cpp src/inference_engine.cpp)
    target_link_libraries(test_batch_shapes Threads::Threads onnxruntime)
    add_test(NAME batch_shapes COMMAND test_batch_shapes)
    set_tests_properties(batch_shapes PROPERTIES TIMEOUT 120)

    add_executable(test_tensor_payload tests/test_tensor_payload.cpp)
    add_test(NAME tensor_payload COMMAND test_tensor_payload)

    add_executable(test_request_validation tests/test_request_validation.cpp)
    add_test(NAME request_validation COMMAND test_request_validation)

    add_executable(test_adaptive_admission tests/test_adaptive_admission.cpp)
    add_test(NAME adaptive_admission COMMAND test_adaptive_admission)
    set_tests_properties(adaptive_admission PROPERTIES TIMEOUT 10)
endif()
