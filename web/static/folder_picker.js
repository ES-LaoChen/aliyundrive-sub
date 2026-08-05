/**
 * folder_picker.js – 共享目录选择器
 * 全局 FolderPicker 类，与 partials/folder_picker.html 配合使用。
 *
 * 支持两种容器模式：
 *   - "modal"：模态对话框（用于 subscription_edit.html）
 *   - "inline"：内联面板（用于 tg_monitor.html）
 *
 * 端点：复用既有 /files/api/drives 与 /files/api/list，无新后端接口。
 */
(function () {
  'use strict';

  /**
   * @class FolderPicker
   * @param {Object} options
   * @param {'modal'|'inline'} options.containerMode - 容器模式
   * @param {string} options.fieldPrefix - 隐藏域前缀
   * @param {string} options.uid - 容器唯一标识
   * @param {string} options.drivesEndpoint - 盘列表端点 URL
   * @param {string} options.listEndpoint - 目录列表端点 URL
   * @param {string} options.initialFolderId - 初始已选 folder_id
   * @param {string} options.initialFolderPath - 初始已选路径
   * @param {string} options.initialDriveType - 初始盘类型
   * @param {string} options.displayInputId - 只读展示输入框 ID
   * @param {string} options.statusElementId - 状态提示元素 ID
   * @param {string} options.openBtnId - 打开按钮 ID
   * @param {string} options.changeBtnId - 更改按钮 ID
   * @param {string} options.clearBtnId - 清空按钮 ID
   * @param {function} [options.onConfirm] - 选择确认回调
   */
  function FolderPicker(options) {
    if (!(this instanceof FolderPicker)) {
      return new FolderPicker(options);
    }

    this.opts = options || {};
    this.mode = this.opts.containerMode || 'modal';
    this.prefix = this.opts.fieldPrefix || 'target';
    this.uid = this.opts.uid || (this.prefix + '-fp');

    // 端点
    this.drivesEndpoint = this.opts.drivesEndpoint || '/files/api/drives';
    this.listEndpoint = this.opts.listEndpoint || '/files/api/list';

    // 状态
    this.drives = [];
    this.currentDrive = this.opts.initialDriveType || 'default';
    this.breadcrumb = [];   // [{id, name}]
    this.selected = null;   // {id, name, path}

    // DOM 缓存
    this._cacheDOM();
    this._bindEvents();
    this._initState();
  }

  /**
   * 缓存 DOM 元素引用
   */
  FolderPicker.prototype._cacheDOM = function () {
    var p = this.prefix;
    var uid = this.uid;

    if (this.mode === 'modal') {
      this.modal = document.getElementById(uid + '-modal');
      this.driveSwitcherEl = document.getElementById(uid + '-drive-switcher');
      this.breadcrumbEl = document.getElementById(uid + '-breadcrumb');
      this.listEl = document.getElementById(uid + '-list');
      this.currentPathEl = document.getElementById(uid + '-current-path');
      this.btnConfirm = document.getElementById(uid + '-confirm');
      this.btnCancel = document.getElementById(uid + '-cancel');
      this.btnClose = document.getElementById(uid + '-close');
      this.selectionHint = document.getElementById(uid + '-selection-hint');
    } else {
      // inline mode
      this.panel = document.getElementById(uid + '-panel');
      this.driveSelect = document.getElementById(uid + '-drive');
      this.crumbsEl = document.getElementById(uid + '-crumbs');
      this.pathEl = document.getElementById(uid + '-path');
      this.listEl = document.getElementById(uid + '-list');
      this.errorEl = document.getElementById(uid + '-error');
      this.chooseBtn = document.getElementById(uid + '-choose');
      this.selectedBox = document.getElementById(uid + '-selected');
      this.selectedText = document.getElementById(uid + '-selected-text');
      this.panelCloseBtn = document.getElementById(uid + '-panel-close');
    }

    // 通用元素
    this.hiddenId = document.getElementById(p + '_folder_id');
    this.hiddenPath = document.getElementById(p + '_folder_path');
    this.hiddenDrive = document.getElementById(p + '_drive_type');
    this.openBtn = document.getElementById(this.opts.openBtnId);
    this.changeBtn = document.getElementById(this.opts.changeBtnId);
    this.clearBtn = document.getElementById(this.opts.clearBtnId);
    this.displayInput = document.getElementById(this.opts.displayInputId);
    this.statusEl = document.getElementById(this.opts.statusElementId);
  };

  /**
   * 绑定事件
   */
  FolderPicker.prototype._bindEvents = function () {
    var self = this;

    // 打开
    if (this.openBtn) {
      this.openBtn.addEventListener('click', function () { self.open(); });
    }
    if (this.changeBtn) {
      this.changeBtn.addEventListener('click', function () { self.open(); });
    }
    if (this.clearBtn) {
      this.clearBtn.addEventListener('click', function () {
        self.clear();
        self._updateStatus();
      });
    }

    if (this.mode === 'modal') {
      if (this.btnClose) {
        this.btnClose.addEventListener('click', function () { self.close(); });
      }
      if (this.btnCancel) {
        this.btnCancel.addEventListener('click', function () { self.close(); });
      }
      if (this.btnConfirm) {
        this.btnConfirm.addEventListener('click', function () { self._confirmSelection(); });
      }
      // 点击模态背景关闭
      if (this.modal) {
        this.modal.addEventListener('click', function (e) {
          if (e.target === self.modal) self.close();
        });
      }
    } else {
      // inline mode
      if (this.panelCloseBtn) {
        this.panelCloseBtn.addEventListener('click', function () { self.close(); });
      }
      if (this.chooseBtn) {
        this.chooseBtn.addEventListener('click', function () { self._confirmSelection(); });
      }
      if (this.driveSelect) {
        this.driveSelect.addEventListener('change', function () {
          self.currentDrive = self.driveSelect.value;
          self._resetToDriveRoot();
          self._loadList();
        });
      }
    }
  };

  /**
   * 初始化状态：如果已有已选目录，对齐 JS 状态
   */
  FolderPicker.prototype._initState = function () {
    if (this.hiddenId && this.hiddenId.value) {
      this.currentDrive = (this.hiddenDrive && this.hiddenDrive.value) || 'default';
      this._updateStatus();

      if (this.mode === 'inline') {
        if (this.selectedBox) this.selectedBox.hidden = false;
        if (this.openBtn) this.openBtn.hidden = true;
      }
      if (this.mode === 'modal') {
        // 模态模式：已选目录时显示“清空”按钮
        if (this.clearBtn) this.clearBtn.hidden = false;
      }
    } else if (this.mode === 'modal') {
      // 模态模式：未选目录时隐藏“清空”按钮
      if (this.clearBtn) this.clearBtn.hidden = true;
    }
  };

  /**
   * 更新状态提示
   */
  FolderPicker.prototype._updateStatus = function () {
    if (this.statusEl) {
      if (this.hiddenPath && this.hiddenPath.value) {
        var driveLabel = '';
        if (this.hiddenDrive && this.hiddenDrive.value) {
          var dMap = { 'default': '默认盘', 'resource': '资源盘', 'backup': '备份盘' };
          driveLabel = dMap[this.hiddenDrive.value] || this.hiddenDrive.value;
        }
        this.statusEl.textContent = '已选择' + (driveLabel ? ' (' + driveLabel + ')' : '') + ': ' + this.hiddenPath.value;
        this.statusEl.style.color = 'var(--ok)';
      } else {
        this.statusEl.textContent = '未选择目录';
        this.statusEl.style.color = 'var(--muted)';
      }
    }
    if (this.displayInput && this.hiddenPath) {
      this.displayInput.value = this.hiddenPath.value || '';
    }
  };

  /**
   * 打开选择器
   */
  FolderPicker.prototype.open = function () {
    var self = this;

    // 重置状态
    this.currentDrive = (this.hiddenDrive && this.hiddenDrive.value) || 'default';
    this.breadcrumb = [];
    this.selected = null;

    if (this.mode === 'modal') {
      if (this.modal) { this.modal.hidden = false; this.modal.classList.add('open'); }
      if (this.btnConfirm) this.btnConfirm.disabled = true;
      if (this.driveSwitcherEl) this.driveSwitcherEl.innerHTML = '';
      if (this.breadcrumbEl) this._renderBreadcrumb();
      if (this.selectionHint) {
        this.selectionHint.textContent = '点击文件夹可进入子目录';
        this.selectionHint.style.color = 'var(--muted)';
      }
      if (this.listEl) this.listEl.innerHTML = '<div class="empty">正在加载盘列表…</div>';
      if (this.currentPathEl) this.currentPathEl.textContent = '/';
    } else {
      if (this.panel) this.panel.hidden = false;
      if (this.openBtn) this.openBtn.hidden = true;
      this._clearError();
      if (!this.breadcrumb.length) {
        this._resetToDriveRoot();
      }
    }

    // 加载盘列表
    this._loadDrives(function () {
      if (self.drives.length) {
        if (self.mode === 'modal') {
          self._renderDriveSwitcher();
          self._navigateTo('root', '/');
        } else {
          self._loadList();
        }
      }
    });
  };

  /**
   * 关闭选择器
   */
  FolderPicker.prototype.close = function () {
    if (this.mode === 'modal') {
      if (this.modal) { this.modal.hidden = true; this.modal.classList.remove('open'); }
    } else {
      if (this.panel) this.panel.hidden = true;
      if (!this.hiddenId || !this.hiddenId.value) {
        if (this.openBtn) this.openBtn.hidden = false;
      }
    }
  };

  /**
   * 获取当前选择
   * @returns {{fileId: string|null, path: string|null, driveType: string|null}}
   */
  FolderPicker.prototype.getSelection = function () {
    return {
      fileId: this.hiddenId ? (this.hiddenId.value || null) : null,
      path: this.hiddenPath ? (this.hiddenPath.value || null) : null,
      driveType: this.hiddenDrive ? (this.hiddenDrive.value || null) : null
    };
  };

  /**
   * 清空选择
   */
  FolderPicker.prototype.clear = function () {
    if (this.hiddenId) this.hiddenId.value = '';
    if (this.hiddenPath) this.hiddenPath.value = '';
    if (this.hiddenDrive) this.hiddenDrive.value = '';
    if (this.displayInput) this.displayInput.value = '';
    if (this.statusEl) {
      this.statusEl.textContent = '未选择目录';
      this.statusEl.style.color = 'var(--muted)';
    }
    if (this.mode === 'inline') {
      if (this.selectedBox) this.selectedBox.hidden = true;
      if (this.openBtn) this.openBtn.hidden = false;
    }
    if (this.mode === 'modal') {
      // 模态模式：清空后隐藏“清空”按钮，打开按钮保持可见
      if (this.clearBtn) this.clearBtn.hidden = true;
    }
  };

  /**
   * 销毁实例（解绑清理）
   */
  FolderPicker.prototype.destroy = function () {
    // 简单解绑 — 移除事件监听的完整实现需追踪所有监听器
    // 此方法标记为可销毁，当前使用场景（SPA 无路由重挂）下直接让 DOM GC 即可
    this.openBtn = null;
    this.changeBtn = null;
    this.clearBtn = null;
    this.modal = null;
    this.panel = null;
    // 从全局实例池移除
    if (window.folderPickerInstance && window.folderPickerInstance[this.uid]) {
      delete window.folderPickerInstance[this.uid];
    }
  };

  // =================== 内部方法 ===================

  /**
   * 加载盘列表
   * @param {function} [callback] - 加载完成回调
   */
  FolderPicker.prototype._loadDrives = function (callback) {
    var self = this;
    fetch(this.drivesEndpoint, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        self.drives = (data && data.drives) || [];
        if (self.mode === 'inline' && self.driveSelect) {
          self._populateDriveSelect();
        }
        if (callback) callback();
      })
      .catch(function (err) {
        if (self.mode === 'inline') {
          self._showError('无法获取云盘列表：' + (err.message || String(err)) + '。请先在「设置」页配置 refresh_token。');
        }
        if (self.mode === 'modal') {
          if (self.listEl) {
            self.listEl.innerHTML =
              '<div class="empty" style="padding:32px 20px">' +
              '<div style="font-size:14px;color:var(--bad);margin-bottom:8px">❌ 无法加载网盘盘列表</div>' +
              '<div style="color:var(--muted);font-size:12px;line-height:1.6">' +
              '可能原因：<br>1. 尚未在<a href="/settings">设置页</a>配置 refresh_token<br>' +
              '2. refresh_token 已过期或无效<br>3. 网络异常' +
              '</div></div>';
          }
          if (self.currentPathEl) self.currentPathEl.textContent = '—';
        }
        if (callback) callback();
      });
  };

  /**
   * 填充盘选择器（inline 模式）
   */
  FolderPicker.prototype._populateDriveSelect = function () {
    this.driveSelect.innerHTML = '';
    this.drives.forEach(function (d) {
      var opt = document.createElement('option');
      opt.value = d.drive_type;
      opt.textContent = (d.drive_name || d.drive_type) + '（' + d.drive_type + '）';
      this.driveSelect.appendChild(opt);
    }, this);
    var matched = this.drives.some(function (d) { return d.drive_type === this.currentDrive; }, this);
    if (matched) {
      this.driveSelect.value = this.currentDrive;
    } else if (this.drives.length) {
      this.currentDrive = this.drives[0].drive_type;
      this.driveSelect.value = this.currentDrive;
    }
  };

  /**
   * 渲染盘切换器（modal 模式）
   */
  FolderPicker.prototype._renderDriveSwitcher = function () {
    if (!this.driveSwitcherEl) return;
    var self = this;
    this.driveSwitcherEl.innerHTML = '';
    this.drives.forEach(function (d) {
      var a = document.createElement('a');
      a.className = 'btn ' + (d.drive_type === self.currentDrive ? 'primary' : 'ghost');
      a.textContent = d.drive_name;
      a.href = 'javascript:void(0)';
      a.onclick = function () {
        if (d.drive_type !== self.currentDrive) {
          self.currentDrive = d.drive_type;
          self.breadcrumb = [];
          self.selected = null;
          if (self.btnConfirm) self.btnConfirm.disabled = true;
          self._renderBreadcrumb();
          self._navigateTo('root', '/');
        }
      };
      self.driveSwitcherEl.appendChild(a);
    });
  };

  /**
   * 渲染面包屑
   */
  FolderPicker.prototype._renderBreadcrumb = function () {
    var container = this.mode === 'modal' ? this.breadcrumbEl : this.crumbsEl;
    if (!container) return;
    var self = this;
    container.innerHTML = '';

    var rootLink = document.createElement('a');
    rootLink.className = this.mode === 'modal' ? 'picker-crumblink' : 'fp-crumb';
    rootLink.textContent = '根目录';
    rootLink.onclick = function (e) {
      if (e) e.preventDefault();
      self.breadcrumb = [];
      self.selected = null;
      if (self.btnConfirm) self.btnConfirm.disabled = true;
      self._renderBreadcrumb();
      self._navigateTo('root', '/');
    };
    container.appendChild(rootLink);

    var sepClass = this.mode === 'modal' ? 'cell-sub' : 'fp-crumb-sep';
    this.breadcrumb.forEach(function (b, i) {
      var sep = document.createElement('span');
      sep.className = sepClass;
      sep.textContent = '/';
      container.appendChild(sep);

      if (i === self.breadcrumb.length - 1) {
        var span = document.createElement('span');
        span.className = self.mode === 'modal' ? 'cell-main' : 'fp-crumb';
        span.textContent = b.name;
        container.appendChild(span);
      } else {
        var link = document.createElement('a');
        link.className = self.mode === 'modal' ? 'picker-crumblink' : 'fp-crumb';
        link.textContent = b.name;
        link.onclick = function (e) {
          if (e) e.preventDefault();
          self.breadcrumb = self.breadcrumb.slice(0, i + 1);
          self.selected = null;
          if (self.btnConfirm) self.btnConfirm.disabled = true;
          self._renderBreadcrumb();
          var displayPath = '/' + self.breadcrumb.map(function (x) { return x.name; }).join('/');
          self._navigateTo(b.id, displayPath);
        };
        container.appendChild(link);
      }
    });

    // inline 模式单独更新 path 显示
    if (this.mode === 'inline' && this.pathEl) {
      this.pathEl.textContent = this._buildPath();
    }
  };

  /**
   * 构建展示路径
   */
  FolderPicker.prototype._buildPath = function () {
    return '/' + this.breadcrumb.map(function (b) { return b.name; }).join('/');
  };

  /**
   * 导航到指定目录（拉取列表）
   * @param {string} parentId - 父目录 ID
   * @param {string} displayPath - 展示路径
   */
  FolderPicker.prototype._navigateTo = function (parentId, displayPath) {
    var self = this;

    if (this.mode === 'modal') {
      if (this.currentPathEl) this.currentPathEl.textContent = displayPath;
      if (this.listEl) this.listEl.innerHTML = '<div class="empty">加载中…</div>';
    } else {
      this._clearError();
    }

    var url = this.listEndpoint +
      '?parent=' + encodeURIComponent(parentId) +
      '&drive=' + encodeURIComponent(this.currentDrive);

    fetch(url, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.error) {
          var hintHtml = data.hint
            ? '<div style="color:var(--muted);font-size:12px;margin-top:6px">💡 ' + self._escapeHtml(data.hint) + '</div>'
            : '';
          if (self.mode === 'modal' && self.listEl) {
            self.listEl.innerHTML =
              '<div class="empty" style="padding:32px 20px">' +
              '<div style="font-size:14px;color:var(--bad);margin-bottom:4px">❌ 加载失败</div>' +
              '<div style="color:var(--muted);font-size:12px">' + self._escapeHtml(data.error) + '</div>' +
              hintHtml + '</div>';
          } else {
            self._showError(data.error + (data.hint ? '（' + data.hint + '）' : ''));
          }
          return;
        }

        var items = (data && data.items) || [];
        if (self.mode === 'modal') {
          self._renderModalList(items, parentId, displayPath);
        } else {
          self._renderInlineList(items);
          self._renderBreadcrumb();
        }
      })
      .catch(function (err) {
        if (self.mode === 'modal' && self.listEl) {
          self.listEl.innerHTML =
            '<div class="empty" style="padding:32px 20px">' +
            '<div style="color:var(--bad)">❌ 网络错误</div>' +
            '<div style="color:var(--muted);font-size:12px;margin-top:4px">' + self._escapeHtml(String(err)) + '</div></div>';
        } else {
          self._showError('无法加载目录：' + (err.message || String(err)));
        }
      });
  };

  /**
   * 渲染模态列表
   */
  FolderPicker.prototype._renderModalList = function (items, parentId, displayPath) {
    var self = this;
    if (!this.listEl) return;

    if (!items.length) {
      this.listEl.innerHTML =
        '<div class="empty">' +
        '<div>该目录下没有子文件夹</div>' +
        '<div class="cell-sub" style="font-size:12px;margin-top:8px">💡 双击空白处或点「选定此目录」选择当前目录</div>' +
        '</div>' +
        '<div class="picker-item picker-select-current" id="' + this.uid + '-select-current">' +
        '<span class="ico">📂</span>' +
        '<span class="name"><strong>' + this._escapeHtml(displayPath) + '</strong></span>' +
        '<span class="cell-sub" style="font-size:11px">选定此目录</span></div>';
      var selCur = document.getElementById(this.uid + '-select-current');
      if (selCur) {
        selCur.onclick = function () {
          self.selected = {
            id: parentId,
            name: displayPath.split('/').filter(Boolean).pop() || '根目录',
            path: displayPath
          };
          if (self.btnConfirm) self.btnConfirm.disabled = false;
          self._confirmSelection();
        };
      }
      return;
    }

    this.listEl.innerHTML = '';
    items.forEach(function (item) {
      var fullPath = displayPath.endsWith('/') ? displayPath + item.name : displayPath + '/' + item.name;
      var row = document.createElement('div');
      row.className = 'picker-item';
      row.innerHTML = '<span class="ico">📁</span><span class="name">' + self._escapeHtml(item.name) + '</span><span class="cell-sub" style="font-size:11px">双击进入</span>';

      row.onclick = function () {
        self.listEl.querySelectorAll('.picker-item.selected').forEach(function (el) { el.classList.remove('selected'); });
        row.classList.add('selected');
        self.selected = { id: item.file_id, name: item.name, path: fullPath };
        if (self.btnConfirm) self.btnConfirm.disabled = false;
        self._renderSelectedHint();
      };
      row.ondblclick = function () {
        self.listEl.querySelectorAll('.picker-item.selected').forEach(function (el) { el.classList.remove('selected'); });
        row.classList.add('selected');
        self.selected = { id: item.file_id, name: item.name, path: fullPath };
        if (self.btnConfirm) self.btnConfirm.disabled = false;
        self.breadcrumb.push({ id: item.file_id, name: item.name });
        self._renderBreadcrumb();
        self._navigateTo(item.file_id, fullPath);
      };
      self.listEl.appendChild(row);
    });

    // 底部"选定当前目录"
    var selCur = document.createElement('div');
    selCur.className = 'picker-item picker-select-current';
    selCur.innerHTML = '<span class="ico">📂</span><span class="name"><strong>选定当前目录 (' + this._escapeHtml(displayPath) + ')</strong></span><span class="cell-sub" style="font-size:11px">使用此层</span>';
    selCur.onclick = function () {
      self.selected = {
        id: parentId,
        name: displayPath === '/' ? '根目录' : (displayPath.split('/').filter(Boolean).pop() || '根目录'),
        path: displayPath
      };
      if (self.btnConfirm) self.btnConfirm.disabled = false;
      self._renderSelectedHint();
      self._confirmSelection();
    };
    this.listEl.appendChild(selCur);
    this._renderSelectedHint();
  };

  /**
   * 渲染内联列表
   */
  FolderPicker.prototype._renderInlineList = function (items) {
    if (!this.listEl) return;
    this.listEl.innerHTML = '';
    var self = this;

    if (!items.length) {
      var empty = document.createElement('li');
      empty.className = 'fp-empty';
      empty.textContent = '（此目录为空）';
      this.listEl.appendChild(empty);
      return;
    }

    items.forEach(function (f) {
      var li = document.createElement('li');
      li.className = 'fp-item';
      var icon = document.createElement('span');
      icon.className = 'fp-folder-icon';
      icon.textContent = '📁';
      li.appendChild(icon);
      li.appendChild(document.createTextNode(f.name));
      li.title = '点击进入「' + f.name + '」';
      li.addEventListener('click', function () {
        self.breadcrumb.push({ file_id: f.file_id, name: f.name });
        self._loadList();
      });
      self.listEl.appendChild(li);
    });
  };

  /**
   * 内联模式加载列表
   */
  FolderPicker.prototype._loadList = function () {
    this._clearError();
    var parent = this.breadcrumb.length ? this.breadcrumb[this.breadcrumb.length - 1].file_id : 'root';
    this._navigateTo(parent, this._buildPath());
  };

  /**
   * 内联模式重置到盘根目录
   */
  FolderPicker.prototype._resetToDriveRoot = function () {
    var rootName = '云盘根目录';
    var match = this.drives.find(function (d) { return d.drive_type === this.currentDrive; }, this);
    if (match) rootName = match.drive_name || match.drive_type;
    this.breadcrumb = [{ file_id: 'root', name: rootName }];
  };

  /**
   * 确认选择
   */
  FolderPicker.prototype._confirmSelection = function () {
    var id, path, driveType;

    if (this.mode === 'modal') {
      if (!this.selected) return;
      id = this.selected.id;
      path = this.selected.path;
      driveType = this.currentDrive || '';
      if (this.modal) { this.modal.hidden = true; this.modal.classList.remove('open'); }
    } else {
      if (!this.breadcrumb.length) return;
      var cur = this.breadcrumb[this.breadcrumb.length - 1];
      id = cur.file_id;
      path = this._buildPath();
      driveType = this.currentDrive;
      if (this.selectedBox) {
        this.selectedText.textContent = path + '（' + driveType + '）';
        this.selectedBox.hidden = false;
      }
      this.close();
    }

    if (this.hiddenId) this.hiddenId.value = id;
    if (this.hiddenPath) this.hiddenPath.value = path;
    if (this.hiddenDrive) this.hiddenDrive.value = driveType;
    if (this.displayInput) this.displayInput.value = path;
    if (this.mode === 'modal' && this.clearBtn) this.clearBtn.hidden = false;
    this._updateStatus();

    // 回调
    if (typeof this.opts.onConfirm === 'function') {
      this.opts.onConfirm({ fileId: id, path: path, driveType: driveType });
    }
  };

  /**
   * 渲染选中提示（modal）
   */
  FolderPicker.prototype._renderSelectedHint = function () {
    if (!this.selectionHint) return;
    if (this.selected) {
      this.selectionHint.textContent = '✓ 已选: ' + this.selected.path;
      this.selectionHint.style.color = 'var(--ok)';
    } else {
      this.selectionHint.textContent = '单击文件夹选中；双击进入子目录；或点底部「选定当前目录」';
      this.selectionHint.style.color = 'var(--muted)';
    }
  };

  /**
   * 显示错误（inline）
   */
  FolderPicker.prototype._showError = function (msg) {
    if (this.errorEl) {
      this.errorEl.textContent = msg;
      this.errorEl.hidden = false;
    }
  };

  /**
   * 清除错误（inline）
   */
  FolderPicker.prototype._clearError = function () {
    if (this.errorEl) {
      this.errorEl.textContent = '';
      this.errorEl.hidden = true;
    }
  };

  /**
   * HTML 转义
   */
  FolderPicker.prototype._escapeHtml = function (s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };

  // 全局注册
  window.FolderPicker = FolderPicker;
  window.folderPickerInstance = {};
})();
