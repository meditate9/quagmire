#################################################
#  Short docker file to distribute some notebooks
#################################################

ARG FROMIMG_ARG
FROM ${FROMIMG_ARG}

#################################################
# Non standard as the files come from the package

ENV MODULE_DIR="miller_alaskamoho_srl2018-src"
ADD --chown=jovyan:jovyan $MODULE_DIR $MODULE_DIR
RUN   cd $MODULE_DIR && pip install .

RUN ipython -m miller_alaskamoho_srl2018 install_notebooks Notebooks

# This file is too big to bundle with the pip installation
ADD  --chown=jovyan:jovyan $MODULE_DIR/miller_alaskamoho_srl2018/Notebooks/ShadedRelief/GRAY_HR_SR_OB.tif Notebooks/ShadedRelief/GRAY_HR_SR_OB.tif

# Non standard as the files come from the package
#################################################


## These are supplied by the build script
## build-dockerfile.sh

ARG IMAGENAME_ARG
ARG PROJ_NAME_ARG
ARG NB_PORT_ARG
ARG NB_PASSWD_ARG
ARG NB_DIR_ARG
ARG START_NB_ARG

# The args need to go into the environment so they
# can be picked up by commands/templates (defined previously)
# when the container runs

ENV IMAGENAME=$IMAGENAME_ARG
ENV PROJ_NAME=$PROJ_NAME_ARG
ENV NB_PORT=$NB_PORT_ARG
ENV NB_PASSWD=$NB_PASSWD_ARG
ENV NB_DIR=$NB_DIR_ARG
ENV START_NB=$START_NB_ARG


## NOW INSTALL NOTEBOOKS

# (This is not standard - nothing to do here )

## The notebooks (and other files we are serving up)
## ADD --chown=jovyan:jovyan  $NB_DIR /home/jovyan/Notebooks

# Trust all notebooks
# RUN find -name \*.ipynb  -print0 | xargs -0 jupyter trust

# expose notebook port server port
EXPOSE $NB_PORT

# # note we use xvfb which to mimic the X display for lavavu
# ENTRYPOINT ["/usr/local/bin/tini", "--", "xvfbrun.sh"]
#
# CMD scripts/run-jupyter.sh
