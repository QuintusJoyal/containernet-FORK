FROM ubuntu:24.04
LABEL maintainer="manuel@peuster.de"
ENV TZ=Europe/PARIS \
    DEBIAN_FRONTEND=noninteractive

# install required packages
RUN apt-get clean
RUN apt-get update \
    && apt-get install -y  git \
    net-tools \
    aptitude \
    build-essential \
    python3-setuptools \
    python3-dev \
    python3-pip \
    python3-venv \
    software-properties-common \
    ansible \
    curl \
    iptables \
    iputils-ping \
    sudo

# install containernet (using its Ansible playbook)
COPY . /containernet
WORKDIR /containernet/ansible
RUN ansible-playbook -i "localhost," -c local --skip-tags "notindocker" install.yml
WORKDIR /containernet
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
RUN make install

# tell containernet that it runs in a container
ENV CONTAINERNET_NESTED=1

# Important: This entrypoint is required to start the OVS service
ENTRYPOINT ["util/docker/entrypoint.sh"]
CMD ["python3", "examples/containernet_example.py"]
