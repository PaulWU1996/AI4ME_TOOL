# Containerised Production Tool for BBC

## 1. Introduction
This project aims to provide a containerised production tool for BBC creative team. It includes understanding algorithms from audio and visual modalities respectively and main control pannel.

```
.
├── audio.txt
├── Dockerfile
├── main.py
├── models
├── README.md
├── src
│   ├── app
│   ├── audio
│   └── visual
└── visual.txt
```
|FILE or FOLDER|EXPLAINATION|
|---|---|
|audio.txt|requirements file for audio understanding algorithm|
|Dockerfile|docker image configuration|
|main.py|main control pannel|
|models|saving ckpt file for audio & visual understanding algorithms, which is mounted to container rather than copied|
|app|project placeholder for main app|
|audio|project placeholder for audio understanding|
|video|project placeholder for visual understanding|
|visual.txt|requirements file for visual understanding algorithm|


## 2. Docker Image 

Docker 

### 2.1 Dockerfile
Docker image is used to compose the environment and code for running service.

|Layer|Requirements|
|---|---|
|Base Image|CUDA12.1, Linux|
|System Dependencies| Miniconda, FFmpeg|
|Python Env|Audio Understanding, Visual Understanding, APP|
|SOURCE|Audio Project (code-only), Visual Project (code-only), app.py|


### 2.2 Workflow
We assume the clients will click the button on UI to trigger the processing start. The control pannel will receive the request and analyze the arguments to excute the specific understanding algorithms or both.

```
[UI] --triger--> [Control Pannel] --excute 
```
