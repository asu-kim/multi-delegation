# Running the Multi-Delegation Experiment

This document explains how to reproduce the delegation experiment included in this repository.

The experiment requires five terminal windows running concurrently:
1. SST environment + Auth server
2. Resource server (target service)
3. External agent
4. Personal agent
5. User

After running all components, the system executes a delegation workflow and automatically records latency logs.

## Prerequisites
Make sure the repository and its submodules are initialized.
```
git clone https://github.com/asu-kim/multi-delegation.git
cd multi-delegation

git submodule update --init --recursive
git pull
```
Before running the experiment, install the following dependencies.

The system requires:

- Python 3
- Node.js
- Java
- Ollama (for running the LLM)
- Redis (for inter-process communication)

### 1. Install required Python packages
The requirements.txt file includes Python libraries used by the experiment scripts
```
pip install -r requirements.txt
```

### 2. Install Redis
Redis is used for communication between the user and agents.

#### macOS (Homebrew)
```
brew install redis
```
#### Ubuntu / Debian
```
sudo apt update
sudo apt install redis-server
```

Start Redis:
```
redis-server
```
Verify Redis is running:
```
redis-cli ping
```
Expected output:
```
PONG
```

#### 3. Install Ollama
Install Ollama from:
[https://ollama.com](https://ollama.com)

or via command line:
```
curl -fsSL https://ollama.com/install.sh | sh
```
Verify installation:
```
ollama --version
```

#### 4. Pull the Required LLM
Download the model used by the agents.
```
ollama pull gpt-oss:20b
```

#### 5. Run the LLM Server
Start the model locally:
```
ollama run gpt-oss:20b
```
The agents will connect to the local Ollama instance to perform reasoning during the delegation workflow.

**⚠️ Make sure the Ollama server is running before starting the agents.**


## Terminal 1 - Initialize the SST and Auth server
Initialize the SST environment:
```
cd iotauth/example

./cleanup.sh
./generateAll -g configs/multi_agent.graph
```
Start the Auth server:
```
cd ../auth/auth-server

make

java -jar target/auth-server-jar-with-dependencies.jar -p ../properties/exampleAuth101.properties
```
The Auth server will now listen for authentication and session-key requests.


## Terminal 2 - Start the Resource Server
Run the resource server that agents will attempt to access.
```
cd iotauth/entity/node/example_entities
node server.js configs/net1/resourceA.config
```
This server represents the protected resource that requires a valid delegated access.


## Terminal 3 — Start the ExternalAgent
Run the external agent experiment script.
```
cd experiment
python3 externalAgent_latency.py
```

## Terminal 4 — Start the PersonalAgent
Run the personal agent experiment script.
```
cd experiment
python3 personalAgent_latency.py
```

## Terminal 5 — Start the User
Run the user script that initiates the delegation workflow.
```
cd experiment
python3 user_latency.py
```

## Experiment Output
After all components are running, the delegation workflow will execute automatically.

Once the process completes, three log files will be generated in the `experiment` directory, corresponding to:

- user execution log (e.g., `user_log_1772589312.txt`)
- personal agent execution log (e.g., `personalAgent_log_1772589307.txt`)
- external agent execution log (e.g., `externalAgent_log_1772589303.txt`)

These logs contain timestamps, Auth's responce, llm's reasoning, and latency measurements for the delegation interactions.
