# multi-delegation

## Overview

Modern cyber-physical systems (CPS) and Internet of Things (IoT) deployments increasingly rely on autonomous entities operating in decentralized edge environments. In such systems, access rights often need to be delegated dynamically: for example, when one autonomous agent temporarily authorizes another to perform a specific task. These delegated permissions must be granted securely, limited by predefined conditions, and revoked correctly once they are no longer valid.

This repository extends the Secure Swarm Toolkit (SST) with a delegation-aware authorization mechanism that supports:

- multi-level access delegation
- dynamic authorization policy updates
- time-bounded delegated permissions
- cascading revocation of delegated privileges

The implementation introduces new authorization metadata and database structures into SST's Auth component, enabling delegated access to be propagated and revoked while preserving delegation provenance and enforcing policy validity.

## Directory structure

- **iotauth/**: Includes SST's Auth component, which serves as the Key Distribution Service (KDS) responsible for generating session keys for delegated access, validating delegation privileges through the Delegation Privilege Table (DPT), and enforcing delegated policies' validity periods.
- **experiments/**: Contains experiment code and collected logs demonstrating delegation and revocation.
  - **part1/**: Recorded baseline and proposed-approach workflow logs, organized into `Auth_logs/` and `Node_logs/`.
  - **part2/**: Warehouse experiment scripts, datasets, generated configurations, and collected results for multi-level delegation and cascading revocation.

For detailed experiment instructions and logs, see [experiments/README.md](experiments/README.md).
