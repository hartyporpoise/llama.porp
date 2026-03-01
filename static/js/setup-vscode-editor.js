(function () {
  'use strict';

  var hintSchema = {};
  var DEFAULT_DEPLOY_SPEC = 'image: nginx:latest\nreplicas: 1\nports:\n  - port: 80\n    name: http';
  var deploySpecEditor = null;
  var deployEditorInitStarted = false;
  var deployThemeObserver = null;

  function el(id) { return document.getElementById(id); }

  function getDeploySpecTheme() {
    return document.documentElement.getAttribute('data-theme') === 'light' ? 'vs' : 'vs-dark';
  }

  function setDeploySpecValue(nextValue) {
    var yamlEl = el('app-spec-yaml');
    var fallbackEl = el('app-spec-yaml-fallback');
    if (yamlEl) yamlEl.value = nextValue;
    if (fallbackEl) fallbackEl.value = nextValue;
    if (deploySpecEditor) deploySpecEditor.setValue(nextValue);
  }

  function useFallbackEditor(yamlEl, fallbackEl, hostEl) {
    if (!fallbackEl) return;
    fallbackEl.value = yamlEl.value || DEFAULT_DEPLOY_SPEC;
    fallbackEl.style.display = 'block';
    if (hostEl) hostEl.style.display = 'none';
    if (fallbackEl.dataset.porpulsionSyncBound !== 'true') {
      fallbackEl.dataset.porpulsionSyncBound = 'true';
      fallbackEl.addEventListener('input', function () { yamlEl.value = fallbackEl.value; });
    }
  }

  function initDeploySpecEditor() {
    var yamlEl = el('app-spec-yaml');
    var fallbackEl = el('app-spec-yaml-fallback');
    var hostEl = el('app-spec-editor');
    if (!yamlEl) return;

    if (!yamlEl.value.trim()) yamlEl.value = DEFAULT_DEPLOY_SPEC;
    if (fallbackEl) {
      fallbackEl.value = yamlEl.value;
      fallbackEl.style.display = 'block';
    }
    if (hostEl) hostEl.style.display = 'none';

    if (deploySpecEditor || deployEditorInitStarted) return;

    if (!window.require || !window.require.config) {
      useFallbackEditor(yamlEl, fallbackEl, hostEl);
      return;
    }

    deployEditorInitStarted = true;
    window.require.config({ paths: { vs: 'https://unpkg.com/monaco-editor@0.52.2/min/vs' } });
    window.require(['vs/editor/editor.main'], function () {
      var monaco = window.monaco;
      deployEditorInitStarted = false;
      if (!monaco) {
        useFallbackEditor(yamlEl, fallbackEl, hostEl);
        return;
      }

      loadDeployHints('/api/openapi.json').then(function (hints) {
        hintSchema = hints || {};
      }).catch(function () {
        hintSchema = {};
      }).then(function () {
        registerYamlHints(monaco);
        deploySpecEditor = monaco.editor.create(hostEl, {
        value: yamlEl.value || DEFAULT_DEPLOY_SPEC,
        language: 'yaml',
        theme: getDeploySpecTheme(),
        minimap: { enabled: false },
        automaticLayout: true,
        scrollBeyondLastLine: false,
        wordWrap: 'on',
        tabSize: 2,
        insertSpaces: true,
        detectIndentation: false,
        quickSuggestions: true,
        suggestOnTriggerCharacters: true
      });

      deploySpecEditor.addAction({
        id: 'porpulsion.toggleLineComment',
        label: 'Toggle YAML line comment',
        keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.Slash],
        run: function (editor) {
          return editor.getAction('editor.action.commentLine').run();
        }
      });

      deploySpecEditor.onDidChangeModelContent(function () {
        var text = deploySpecEditor.getValue();
        yamlEl.value = text;
      });
      hostEl.style.display = 'block';
      if (fallbackEl) fallbackEl.style.display = 'none';
      yamlEl.value = deploySpecEditor.getValue();

      if (!deployThemeObserver && window.MutationObserver) {
        deployThemeObserver = new MutationObserver(function () {
          if (window.monaco && deploySpecEditor) window.monaco.editor.setTheme(getDeploySpecTheme());
        });
        deployThemeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
      }
      });
    }, function () {
      deployEditorInitStarted = false;
      useFallbackEditor(yamlEl, fallbackEl, hostEl);
    });
  }

  function resolveRef(specDoc, schema) {
    var cur = schema;
    while (cur && cur.$ref && cur.$ref.indexOf('#/') === 0) {
      var path = cur.$ref.slice(2).split('/');
      var resolved = specDoc;
      for (var i = 0; i < path.length; i++) resolved = resolved[path[i]];
      cur = resolved;
    }
    return cur;
  }

  function getRemoteAppSpecSchema(specDoc) {
    var paths = specDoc.paths;
    if (!paths) return null;
    var postOp = paths['/remoteapp'] && paths['/remoteapp'].post;
    if (!postOp || !postOp.requestBody || !postOp.requestBody.content) return null;
    var content = postOp.requestBody.content['application/json'] || postOp.requestBody.content[Object.keys(postOp.requestBody.content)[0]];
    if (!content || !content.schema) return null;
    var requestSchema = content.schema;
    var resolvedRequest = resolveRef(specDoc, requestSchema);
    if (!resolvedRequest || !resolvedRequest.properties || !resolvedRequest.properties.spec) return null;
    return resolveRef(specDoc, resolvedRequest.properties.spec);
  }

  function buildHintsFromSpec(specSchema) {
    var hints = {};
    if (!specSchema || !specSchema.properties) return hints;
    var required = specSchema.required || [];
    Object.keys(specSchema.properties).forEach(function (key) {
      var prop = specSchema.properties[key];
      var kind = (prop && prop.type) ? prop.type : 'field';
      var req = required.indexOf(key) !== -1;
      hints[key] = {
        detail: (req ? 'Required ' : 'Optional ') + kind,
        docs: (prop && prop.description) ? prop.description : ('Field `' + key + '`.')
      };
    });
    return hints;
  }

  function loadDeployHints(openApiUrl) {
    return fetch(openApiUrl, { credentials: 'same-origin' }).then(function (res) {
      if (!res.ok) return null;
      return res.json().then(function (specDoc) {
        var specSchema = getRemoteAppSpecSchema(specDoc);
        return specSchema ? buildHintsFromSpec(specSchema) : {};
      });
    }).catch(function () { return null; });
  }

  function registerYamlHints(monaco) {
    if (!monaco || window.__porpulsionYamlHintsRegistered) return;
    window.__porpulsionYamlHintsRegistered = true;

    monaco.languages.registerCompletionItemProvider('yaml', {
      triggerCharacters: ['\n', ' ', ':'],
      provideCompletionItems: function (model, position) {
        var keySuggestions = Object.keys(hintSchema).map(function (key) {
          return {
            label: key,
            kind: monaco.languages.CompletionItemKind.Property,
            insertText: key + ': ',
            detail: hintSchema[key].detail,
            documentation: { value: hintSchema[key].docs }
          };
        });
        var word = model.getWordUntilPosition(position);
        var range = {
          startLineNumber: position.lineNumber,
          endLineNumber: position.lineNumber,
          startColumn: word.startColumn,
          endColumn: word.endColumn
        };
        return {
          suggestions: keySuggestions.map(function (item) {
            return Object.assign({}, item, { range: range });
          })
        };
      }
    });

    monaco.languages.registerHoverProvider('yaml', {
      provideHover: function (model, position) {
        var word = model.getWordAtPosition(position);
        if (!word || !hintSchema[word.word]) return null;
        var spec = hintSchema[word.word];
        return {
          range: new monaco.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn),
          contents: [
            { value: '**' + word.word + '**' },
            { value: spec.docs }
          ]
        };
      }
    });
  }

  window.PorpulsionVscodeEditor = {
    initDeploySpecEditor: initDeploySpecEditor,
    setDeploySpecValue: setDeploySpecValue,
    getDefaultDeploySpec: function () { return DEFAULT_DEPLOY_SPEC; },
    registerYamlHints: registerYamlHints
  };
})();
