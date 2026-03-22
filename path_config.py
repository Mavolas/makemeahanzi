#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
剪映工作流路径配置：compose_video_v1 / compose_video_v2 等脚本统一从此处读取。
修改本文件即可切换素材目录与草稿保存位置。
"""

# 剪辑工作流根目录（王者素材、中间文本、音乐、书籍素材、背景文本等均在其子目录下）
BASE_DIR = r"E:\剪辑工作流"

# generate_story_from_txt 默认输出根目录 = BASE_DIR / 下列子目录
STORY_OUTPUT_SUBDIR = "中间文本"

# 可选：放在 BASE_DIR 下，内含背景图时 story 生成会以 5:1 权重相对纯色随机抽一张铺画布（cover）
STORY_BG_IMAGES_SUBDIR = "中间文本背景"

# 其下：HTML 等生成物在「{安全文件名}{STORY_DRAFT_SUFFIX}」；导出 MP4 在「{安全文件名}{STORY_FINAL_SUFFIX}」
STORY_DRAFT_SUFFIX = "_草稿"
STORY_FINAL_SUFFIX = "_成稿"

