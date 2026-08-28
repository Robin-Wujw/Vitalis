AppSettingsPage({
  build(props) {
    const storage = props.settingsStorage;
    return View({ style: { padding: "12px 20px" } }, [
      TextInput({
        label: "Vitalis HTTPS 地址",
        value: storage.getItem("vitalisBase") || "",
        maxLength: 256,
        onChange: (value) => storage.setItem("vitalisBase", value.trim()),
      }),
      TextInput({
        label: "设备上传令牌",
        value: storage.getItem("deviceLinkToken") || "",
        maxLength: 256,
        onChange: (value) => storage.setItem("deviceLinkToken", value.trim()),
      }),
    ]);
  },
});
