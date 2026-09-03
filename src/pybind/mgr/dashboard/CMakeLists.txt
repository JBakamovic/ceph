install(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  DESTINATION ${CEPH_INSTALL_DATADIR}/mgr
  ${mgr_module_install_excludes}
  PATTERN "frontend/*" EXCLUDE
  PATTERN ".*" EXCLUDE)

if(WITH_MGR_DASHBOARD_FRONTEND)
  set(cached_dist "")
  if(WITH_MGR_DASHBOARD_FRONTEND_CACHE)
    get_filename_component(dashboard_cache_dir "${WITH_MGR_DASHBOARD_FRONTEND_CACHE}" ABSOLUTE)
    set(cached_dist "${dashboard_cache_dir}/dist")
  endif()

  set(cached_index "")
  if(cached_dist AND EXISTS "${cached_dist}")
    file(GLOB cached_index "${cached_dist}/*/index.html" "${cached_dist}/index.html")
  endif()

  if(cached_index AND NOT "${CMAKE_CURRENT_SOURCE_DIR}/frontend/package.json" IS_NEWER_THAN "${cached_dist}")
    message(STATUS "Using cached dashboard frontend from ${cached_dist}")
    set(frontend_binary_dir "${CMAKE_CURRENT_BINARY_DIR}/frontend")
    file(MAKE_DIRECTORY "${frontend_binary_dir}")
    file(CREATE_LINK "${cached_dist}" "${frontend_binary_dir}/dist" SYMBOLIC)
    file(COPY "${CMAKE_CURRENT_SOURCE_DIR}/frontend/package.json" DESTINATION "${frontend_binary_dir}")

    add_custom_target(mgr-dashboard-frontend-build)
    if(TARGET tests)
      add_dependencies(tests mgr-dashboard-frontend-build)
    endif()

    install(DIRECTORY ${frontend_binary_dir}/dist
      DESTINATION ${CEPH_INSTALL_DATADIR}/mgr/dashboard/frontend)
    install(FILES ${frontend_binary_dir}/package.json
      DESTINATION ${CEPH_INSTALL_DATADIR}/mgr/dashboard/frontend)

    if(WITH_TESTS)
      include(AddCephTest)
      add_tox_test(mgr-dashboard-py3 TOX_ENVS py3)
      add_tox_test(mgr-dashboard-lint TOX_ENVS lint)
      add_tox_test(mgr-dashboard-check TOX_ENVS check)
      add_tox_test(mgr-dashboard-openapi TOX_ENVS openapi-check)
    endif()
  else()
    # build from source
    add_subdirectory(frontend)
    if(WITH_TESTS)
      include(AddCephTest)
      add_tox_test(mgr-dashboard-py3 TOX_ENVS py3)
      add_tox_test(mgr-dashboard-lint TOX_ENVS lint)
      add_tox_test(mgr-dashboard-check TOX_ENVS check)
      add_tox_test(mgr-dashboard-openapi TOX_ENVS openapi-check)
    endif()
  endif()
else()
  if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/frontend/dist)
    # prebuilt
    install(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/frontend/dist
      DESTINATION ${CEPH_INSTALL_DATADIR}/mgr/dashboard/frontend)
    install(FILES frontend/package.json
      DESTINATION ${CEPH_INSTALL_DATADIR}/mgr/dashboard/frontend)
  endif()
endif()
