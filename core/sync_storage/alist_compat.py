"""AList 兼容小工具（仅移植 checkExs 排除逻辑）。

移植自 TaoSync ``service/alist/alistClient.py`` 的 ``checkExs``：按 gitignore 风格
``spec`` 排除目录列表中匹配的项。``TaoSyncClient.file_list_detail_api`` 在带排除规则
时使用它，外部 AList 客户端不再需要（本项目仅内置引擎）。
"""


def check_exs(path, rts, spec):
    """按 spec 排除目录列表中的匹配项。

    :param path: 当前所在的相对根路径（以 ``/`` 分隔，可能为空）。
    :param rts: 内容列表，key 以 ``/`` 结尾表示目录，否则为文件（值为大小）。
    :param spec: pathspec.PathSpec 排除规则。
    :return: 排除后的内容列表副本。
    """
    rts_new = rts.copy()
    base_path = str(path or "").strip("/")
    for rts_item in list(rts.keys()):
        candidate = "/".join(part for part in (base_path, rts_item) if part)
        if spec.match_file(candidate):
            del rts_new[rts_item]
    return rts_new
