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
  # librados normally ships an ABI-stable .so: symbol versioning (the .symver
  # machinery in librados_c.cc, enabled by -DLIBRADOS_SHARED, plus the
  # librados.map version script) defines the public ABI, and
  # -Wl,--exclude-libs,ALL hides the internal symbols pulled in from the static
  # librados_impl archive. Two development-only builds must relax this:
  #
  #  - CEPH_USE_LINKER (lld/mold/gold): the version-script scheme uses GNU ld
  #    semantics those linkers reject, so drop versioning.
  #  - WITH_UBSAN: UBSAN's -fsanitize=vptr emits cross-DSO references to the
  #    typeinfo of internal polymorphic classes (e.g. RadosClient). Those
  #    typeinfos are weak symbols in librados_impl.a, but --exclude-libs,ALL and
  #    the version script's `local: *` keep them out of librados.so's dynamic
  #    table, so librbd/pybind fail with "undefined reference to typeinfo for
  #    librados::v14_2_0::RadosClient". Export them by dropping BOTH.
  #
  # In every such case the resulting librados.so is dev-only and NOT ABI-stable.
  if(CEPH_USE_LINKER OR WITH_UBSAN)
    set(_librados_versioned OFF)
    message(STATUS "librados: symbol versioning disabled (dev build; "
      "CEPH_USE_LINKER='${CEPH_USE_LINKER}', WITH_UBSAN=${WITH_UBSAN}); "
      "librados.so is NOT ABI-stable")
  else()
    set(_librados_versioned ON)
  endif()
  # Hide internal symbols normally; export them under UBSAN so its cross-DSO
  # vptr typeinfo references (e.g. for RadosClient) resolve at link time.
  if(HAVE_LINK_EXCLUDE_LIBS AND NOT WIN32 AND NOT WITH_UBSAN)
    set_property(TARGET librados APPEND_STRING PROPERTY
      LINK_FLAGS " -Wl,--exclude-libs,ALL")
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
