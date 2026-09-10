# 05 — AI Media Ingestion and Organization

## Goal

Turn unstructured client photo/video dumps into a searchable, auditable media library without risking original files.

## Pipeline

```text
Camera / upload
      |
      v
00-inbox
      |
virus/type/basic validation
      |
checksum + immutable raw copy
      |
metadata extraction
      |
thumbnail/proxy generation
      |
vision model analysis
      |
structured tags + proposed filename
      |
confidence / policy gate
      |
catalog/index
      |
human correction where needed
      |
selected / edits / exports
```

## Critical rule

**Never make destructive AI renaming the first version.**

V1 should:
- preserve original bytes;
- compute SHA-256;
- retain original filename;
- write a sidecar/manifest;
- propose canonical filename;
- allow reversible materialization of an organized view.

## Metadata fields

Useful fields include:
- asset ID;
- SHA-256;
- client;
- project;
- captured timestamp;
- original filename;
- canonical filename;
- media type;
- orientation/aspect ratio;
- people count (avoid identity recognition unless explicitly justified and consented);
- scene;
- objects;
- service/product;
- shot type;
- quality flags;
- transcript summary for video;
- model used;
- model version;
- confidence;
- human-reviewed flag;
- created/updated timestamps.

## Retrieval objective

Support queries such as:
- "vertical Stone Bellisimo countertop installation clips";
- "wide kitchen-island shots from the September shoot";
- "all footage tagged sink installation";
- "photos suitable for an Instagram carousel."

## Model design

Separate:
1. low-cost metadata/exif extraction;
2. thumbnail/keyframe generation;
3. vision labeling;
4. filename synthesis;
5. embedding/indexing.

Do not run a large VLM over every frame of every video.

For video, start with:
- technical metadata;
- scene-change/keyframe extraction;
- audio transcript if relevant;
- bounded representative-frame analysis.

## Corrections

Human corrections should become durable ground truth and later evaluation data. Never silently overwrite them with future model output.
