# multi-delegation

This repository contains the experimental implementation of a multi-step delegation workflow built on top of the Secure Swarm Toolkit (SST).
The project explores how an entity can securely delegate its access across multiple agents while ensuring predefined conditions.


# Directory structure

- **iotauth**: Includes the Secure Swarm Toolkit (SST) Auth component as a Git submodule.
This serves as the Key Distribution Service (KDS) responsible for:
  - generating session keys for delegated access
  - validating delegation privilege through `Delegation Privilege Table (DPT)`
  - enforcing new policies' validity periods
- **experiment**: Contains runnable experiment code and collected logs for demonstrating the user/agent workflow.
  - **Entity scripts**: Measures and logs execution latency
    - `user_latency.py`: user-side workflow who will delegate its access to `resource` to `personalAgnet`
    - `personalAgent_latency.py`: personalAgent-side workflow who will get access of the `resource` from the `user` and will delegate its access to `externalAgent`
    - `externalAgent_latency.py`: externalAgent-side workflow who will get access from `personalAgent` and will access to the `resoure`
  - **logs**: `logs/` stores experiment runs grouped by run directory (e.g., `log1`, `log2`)
    - Each run directory contains timestamed logs such as `user_log_*.txt`, `personalAgent_log_*.txt`, and `externalAgent_log_*.txt`
  
Detailed instructions for reproducing our experiments can be found in [experiment/README.md](experiment/README.md).
