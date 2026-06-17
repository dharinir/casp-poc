# Looker Root Cause Analysis (RCA) Agent

This repository contains a desktop web application that embeds an autonomous AI data analyst to investigate Looker stockout alerts. The agent is driven by the Gemini Model via the Jetski (Antigravity) SDK and communicates with Looker and BigQuery via Model Context Protocol (MCP).

## Prerequisites

1.  **Python 3.10+**
2.  **Google Cloud Credentials**:
    *   Ensure you have configured your local environment to access Google Cloud (e.g. via Application Default Credentials: `gcloud auth application-default login`).
    *   The agent uses BigQuery and discovery APIs for metadata lookup.
3.  **Looker SDK Credentials**:
    *   Copy `looker.ini.example` to `looker.ini` and populate it with your Looker instance URL, Client ID, and Client Secret.
4.  **Jetski SDK Wheel**:
    *   This project depends on the Google Antigravity SDK (`jetski_sdk`), which is distributed as a Python wheel.
    *   To build it from google3, run:
        ```bash
        blaze build //third_party/jetski/sdk/py/wheel:jetski_sdk_wheel
        ```
    *   Copy the resulting `.whl` file (from `blaze-bin/third_party/jetski/sdk/py/wheel/`) into this directory.

## Installation

1.  Create a virtual environment:
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```
2.  Install the required dependencies:
    ```bash
    pip install -r requirements.txt
    ```
3.  Install the Jetski SDK:
    ```bash
    pip install jetski_sdk-*.whl
    ```

## Configuration

1.  Configure Looker access:
    ```bash
    cp looker.ini.example looker.ini
    # Open looker.ini and fill in your credentials
    ```

## Running the Application

1.  Start the FastAPI server:
    ```bash
    python3 main.py
    ```
2.  Open your browser and navigate to:
    ```
    http://localhost:8000
    ```
