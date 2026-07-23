add_library(neorados_objs OBJECT
  RADOSImpl.cc)
target_compile_definitions(neorados_objs PRIVATE
  $<TARGET_PROPERTY:${FMT_LIB},INTERFACE_COMPILE_DEFINITIONS>)
target_link_libraries(neorados_objs legacy-option-headers)
add_library(neorados_api_obj OBJECT
  RADOS.cc)
target_compile_definitions(neorados_api_obj PRIVATE
  $<TARGET_PROPERTY:${FMT_LIB},INTERFACE_COMPILE_DEFINITIONS>)
target_link_libraries(neorados_api_obj legacy-option-headers)

add_library(libneorados STATIC
  $<TARGET_OBJECTS:neorados_api_obj>
  $<TARGET_OBJECTS:neorados_objs>)
target_link_libraries(libneorados PRIVATE
  osdc ceph-common cls_lock_client ${FMT_LIB}
  ${BLKID_LIBRARIES} ${CRYPTO_LIBS} ${EXTRALIBS})
if(WITH_UBSAN)
  # RADOS.cc's make_with_librados() bridge reaches into a classic
  # librados::Rados (rados.client, a librados::RadosClient*). Under UBSAN's
  # -fsanitize=vptr that becomes a real link-time reference to
  # librados::RadosClient's typeinfo. librados exports that typeinfo under
  # UBSAN (see src/librados/CMakeLists.txt), so link librados here to resolve it
  # and keep the vptr check enabled on RADOS.cc. Non-UBSAN builds do not emit
  # the reference and stay decoupled from classic librados. This is a static
  # lib, so the dependency propagates to consumers (the neorados tool, tests).
  # No cycle: librados only friend-declares neorados::detail::RadosClient.
  target_link_libraries(libneorados PRIVATE librados)
endif()
