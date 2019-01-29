# /usr/bin/env bash


DOCKER=docker

$DOCKER container run --restart=always --name s5p -v `pwd`:/s5p/ -w /s5p/ -p 8080:6666 -d pypy ./pypy.sh
