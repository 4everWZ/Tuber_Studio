# 大肥鱼 V6 Jetson 部署实测

V6 的 Q4_K_M 和 4× F16 投影器与工作站导出逐字节一致。节点运营档和电脑业务配置使用 `MiniCPM-V-4.7-1B-Dafeiyu-Grounded`，大肥鱼正文沿用 88 字短卡。电脑按实际端点完成三帧、每帧最多 1024×1024 像素、总计 1 MiB 的视觉资源检查，采样期间可用内存最低 3.20 GiB，换页增量为零。[权重、角色卡与版本清单](https://huggingface.co/4everImmortality/dafeiyu-minicpmv/tree/dc0d3e1f5b61d86969957192b2b9b55f08a721fd/v6) · [认证端点原答与资源记录](jetson-operations.json)

认证视觉端点正确识别了红白条纹纸袋。文本端点把 DNS 解释成了密码校验。三帧资源检查和单张图像用例分别检验资源与图像输入，真实视频事实、引用和评论质量继续以逐例回放评估。[工作站独立视频对照](../grounded-persona-v6-20261105/README.md)

隔离探针释放空闲运营进程，使用相同 V6、投影器和 q4_0 KV 打开 131072 窗口。完整处理 130,914 输入 tokens，183.661 秒后答出开头代号「珊瑚-7319」。Genie 在长输入处理期间用 3.696 秒合成非静音语音，全程最低可用内存 2243.4 MiB，换页增量为零。结束后恢复 V6 运营档。另经正式节点切换到 MiniCPM5-2B＋Genie 就绪，再回到 V6。浏览器首音、直播混合负载和同批刷视频热态 p95 留在场景验收。[共驻原始结果](jetson-128k-genie.json) · [运营恢复记录](jetson-restore.json) · [正式模式回切](jetson-mode-roundtrip.json)

恢复上一版时使用保留的节点配置、容器与旧版权重，电脑侧的原设置与视觉验证证明已有备份。[部署与回退步骤](../../../docs/guides/deployment.md#电脑业务主机与独立推理节点)
