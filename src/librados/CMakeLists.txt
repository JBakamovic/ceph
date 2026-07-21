add_library(librados_impl STATIC
  IoCtxImpl.cc
  RadosXattrIter.cc
  RadosClient.cc
  librados_util.cc
  librados_tp.cc)
target_link_libraries(librados_impl
  PRIVATE legacy-option-headers)
# C/C++ API
add_library(librados ${CEPH_SHARED}
  librados_c.cc
  librados_cxx.cc
  $<TARGET_OBJECTS:common_buffer_obj>)
if(ENABLE_SHARED)
  set_target_properties(librados PROPERTIES
    OUTPUT_NAME rados
    VERSION 2.0.0
    SOVERSION 2
    VISIBILITY_INLINES_HIDDEN ON)
  if(HAVE_LINK_EXCLUDE_LIBS AND NOT WIN32)
    set_property(TARGET librados APPEND_STRING PROPERTY
      LINK_FLAGS " -Wl,--exclude-libs,ALL")
  endif()
  # librados ships an ABI-stable .so via symbol versioning: the .symver
  # machinery in librados_c.cc (enabled by -DLIBRADOS_SHARED) plus the
  # librados.map version script. That scheme relies on GNU ld semantics
  # (unversioned "@@" base symbols, literal extern "C++" names) that lld and
  # some other linkers reject. Selecting an alternate linker via
  # CEPH_USE_LINKER is a development/test-only build where ABI stability does
  # not matter, so disable versioning there and fall back to the already
  # supported non-versioned code path, letting librados link with any linker.
  if(CEPH_USE_LINKER)
    set(_librados_versioned OFF)
    message(STATUS "librados: symbol versioning disabled for dev linker "
      "'${CEPH_USE_LINKER}'; the resulting librados.so is NOT ABI-stable")
  else()
    set(_librados_versioned ON)
  endif()
  if(_librados_versioned AND HAVE_LINK_VERSION_SCRIPT AND NOT WIN32)
    set_property(TARGET librados APPEND_STRING PROPERTY
      LINK_FLAGS " -Wl,--version-script=${CMAKE_CURRENT_SOURCE_DIR}/librados.map")
  endif()
  if(WITH_STATIC_LIBSTDCXX)
    set_property(TARGET librados APPEND_STRING PROPERTY
      LINK_FLAGS " -static-libstdc++ -static-libgcc")
  endif()
  if(_librados_versioned)
    target_compile_definitions(librados PRIVATE -DLIBRADOS_SHARED=1)
  endif()
  unset(_librados_versioned)
endif()
target_link_libraries(librados PRIVATE
  librados_impl osdc ceph-common cls_lock_client
  ${BLKID_LIBRARIES} ${CRYPTO_LIBS} ${EXTRALIBS} ${GSSAPI_LIBRARIES})
install(TARGETS librados DESTINATION ${CMAKE_INSTALL_LIBDIR})

if(WITH_LTTNG)
  add_dependencies(librados_impl librados-tp)
  if(WITH_EVENTTRACE)
    add_dependencies(librados_impl eventtrace_tp)
  endif()
endif()
