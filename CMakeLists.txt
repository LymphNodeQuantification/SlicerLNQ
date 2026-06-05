cmake_minimum_required(VERSION 3.16.3...3.19.7 FATAL_ERROR)

project(SlicerLNQ)

#-----------------------------------------------------------------------------
# Extension metadata
#-----------------------------------------------------------------------------
set(EXTENSION_HOMEPAGE "https://github.com/pieper/SlicerLNQ")
set(EXTENSION_CATEGORY "Quantification")
set(EXTENSION_CONTRIBUTORS "Steve Pieper (Isomics)")
set(EXTENSION_DESCRIPTION "Lymph-node quantification workflow against a SlicerLNQ-Chronicle backend (CouchDB + dicomweb-server). Provides the LNQStudio module: Config, Cohorts, Protocols, Projects, Annotate, and (later) Train/Infer/Review/Deploy/Dashboard tabs.")
set(EXTENSION_ICONURL "https://raw.githubusercontent.com/pieper/SlicerLNQ/main/LNQStudio/Resources/Icons/LNQStudio.png")
set(EXTENSION_SCREENSHOTURLS "")
set(EXTENSION_DEPENDS "NA")

#-----------------------------------------------------------------------------
# Extension dependencies
#-----------------------------------------------------------------------------
find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

#-----------------------------------------------------------------------------
# Extension modules
#-----------------------------------------------------------------------------
add_subdirectory(LNQStudio)
add_subdirectory(LymphNodeQuantifier)

#-----------------------------------------------------------------------------
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
