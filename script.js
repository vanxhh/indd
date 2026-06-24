/**
 * extract-images/script.js  ExtendScript (ES3)
 *
 * Runs inside Adobe InDesign Server via the Custom Scripts API.
 *
 * CRITICAL: ExtendScript is ES3. JSON object does NOT exist.
 *   - Parse:     use eval('(' + str + ')')
 *   - Stringify: use the hand-rolled jsonStringify() below
 *   Never call JSON.parse() or JSON.stringify()  they will throw
 *   "JSON is undefined" at runtime.
 *
 * Handles three classes of image in an INDD:
 *
 *   1. LINKED images  (link.status == LinkStatus.LINK_NORMAL or LINK_OUT_OF_DATE)
 *      The source file already exists on disk. Copy it straight to output.
 *
 *   2. EMBEDDED images  (link.status == LinkStatus.LINK_EMBEDDED)
 *      The image data is baked into the INDD. Call link.unembed(outFolder)
 *      which writes the original file to disk, then copy it.
 *      NOTE: collect all embedded links BEFORE iterating  unembed() changes
 *      the links collection length and invalidates numeric indices.
 *
 *   3. PASTED images  (graphic.itemLink === null)
 *      Pasted images never appear in doc.links at all.
 *      Access via doc.allGraphics, check itemLink == null, then call
 *      graphic.exportFile(ExportFormat.JPG, destFile) to save as JPEG.
 *
 * Input   injected by the Custom Scripts API via app.scriptArgs "parameters"
 *   workingFolder         : absolute path to the job's working directory
 *   params.targetDocument : .indd filename inside workingFolder
 *
 * Output JSON string returned from main()
 *   SUCCESS:
 *     { "status":"SUCCESS",
 *       "assetsToBeUploaded":[{"path":"&lt;rel&gt;","data":{...}},...],
 *       "dataURL":"summary.json" }
 *   FAILURE:
 *     { "status":"FAILURE", "errorCode":"...", "errorString":"..." }
 */

// globals
var workingFolder = '';

// ES3-safe JSON polyfill

function jsonParse(str) {
    // eval() is the only option in ES3 JSON object does not exist
    return eval('(' + str + ')');
}

function quoteStr(s) {
    s = String(s);
    s = s.replace(/\\/g, '\\\\');
    s = s.replace(/"/g,  '\\"');
    s = s.replace(/\n/g, '\\n');
    s = s.replace(/\r/g, '\\r');
    s = s.replace(/\t/g, '\\t');
    return '"' + s + '"';
}

function jsonStringify(val) {
    if (val === null || val === undefined) return 'null';
    if (val === true)                      return 'true';
    if (val === false)                     return 'false';
    if (typeof val === 'number')           return isFinite(val) ? String(val) : 'null';
    if (typeof val === 'string')           return quoteStr(val);

    if (val instanceof Array) {
        var items = [];
        for (var i = 0; i < val.length; i++) {
            items.push(jsonStringify(val[i]));
        }
        return '[' + items.join(',') + ']';
    }

    if (typeof val === 'object') {
        var pairs = [];
        for (var k in val) {
            if (val.hasOwnProperty(k)) {
                pairs.push(quoteStr(k) + ':' + jsonStringify(val[k]));
            }
        }
        return '{' + pairs.join(',') + '}';
    }

    return 'null';
}

// return-value builders

function buildSuccess(assets, summaryObj) {
    // Write summary to disk; return relative path as dataURL
    var summaryPath = workingFolder + '\\summary.json';
    var f = new File(summaryPath);
    f.encoding = 'UTF8';
    f.open('write');
    f.write(jsonStringify(summaryObj));
    f.close();

    var result = {};
    result.status              = 'SUCCESS';
    result.assetsToBeUploaded  = assets;
    result.dataURL             = 'summary.json';   // relative to workingFolder
    return jsonStringify(result);
}

function buildFailure(code, msg) {
    var result = {};
    result.status      = 'FAILURE';
    result.errorCode   = code;
    result.errorString = msg;
    return jsonStringify(result);
}

// helpers

function safeFilename(name) {
    // Replace characters illegal on Windows filesystems
    return String(name).replace(/[\/\\:*?"&lt;&gt;|\s]+/g, '_');
}

function getExt(filePath) {
    var s     = String(filePath);
    var parts = s.split('.');
    if (parts.length > 1) {
        return '.' + parts[parts.length - 1].toLowerCase();
    }
    return '';
}

function copyFileTo(srcFile, destPath) {
    try {
        var dest = new File(destPath);
        srcFile.copy(dest);
        return dest.exists;
    } catch (e) {
        return false;
    }
}

/**
 * Walk up the InDesign DOM from a page item to find the parent Page.
 * Returns the page label (name) as a string, or 'unknown'.
 *
 * Reliable approach: use .parentPage property available on most page items,
 * falling back to a manual DOM walk.
 */
function getPageLabel(item) {
    try {
        // .parentPage is the cleanest approach and works on most page items
        if (item.parentPage !== undefined && item.parentPage !== null) {
            return String(item.parentPage.name);
        }
    } catch (ignore) {}

    // Manual DOM walk fallback
    try {
        var p = item;
        for (var depth = 0; depth < 20; depth++) {
            try {
                // Check if p is a Page by testing for a page-specific property
                // Pages have .documentOffset; spreads and other containers don't
                var testOffset = p.documentOffset;
                if (testOffset !== undefined) {
                    return String(p.name);
                }
            } catch (ignore2) {}
            try {
                p = p.parent;
                if (!p) break;
            } catch (e) {
                break;
            }
        }
    } catch (e) {}

    return 'unknown';
}

// image extraction: linked & embedded (via doc.links)

/**
 * Process all linked and embedded images from doc.links.
 *
 * Strategy for embedded:
 *   - Collect all embedded links into an array FIRST (snapshot).
 *   - Then unembed each one. This avoids the "shifting index" problem
 *     where unembed() removes an entry from the live links collection.
 *
 * Strategy for linked:
 *   - The source file path is available via link.filePath.
 *   - Copy the file directly to the output folder.
 */
function processLinkedAndEmbedded(doc, outFolder, outputSubDir, assets, summary, counter) {
    var links = doc.links;

    // Step 1: snapshot all links (embedded + linked)
    // We must collect into a plain array before calling unembed() on any of
    // them, because unembed() modifies the live links collection.
    var allLinks = [];
    var i;
    for (i = 0; i < links.length; i++) {
        allLinks.push(links[i]);
    }

    // Step 2: unembed all embedded links first
    for (i = 0; i < allLinks.length; i++) {
        var lnk = allLinks[i];
        try {
            if (lnk.status === LinkStatus.LINK_EMBEDDED) {
                // unembed(folder) writes the file into outFolder and relinks
                lnk.unembed(outFolder);
                // allLinks[i] still refers to the same Link object; after
                // unembed its status changes to LINK_NORMAL and filePath is set
            }
        } catch (e) {
            // unembed can fail if the format is unusual; record and continue
            var uname = 'unknown';
            try { uname = lnk.name; } catch(ignore) {}
            summary.skipped.push({ name: uname, reason: 'unembed_failed', detail: String(e) });
        }
    }

    // Step 3: copy each (now-linked) file to output
    // Re-read doc.links after unembed operations are complete
    var freshLinks = doc.links;
    for (i = 0; i < freshLinks.length; i++) {
        var link     = freshLinks[i];
        var linkName = 'link_' + i;
        try { linkName = link.name; } catch(ignore) {}

        var pageLabel = 'unknown';
        try { pageLabel = getPageLabel(link.parent); } catch(ignore) {}

        try {
            var srcFile = new File(link.filePath);

            // If the file is not at its absolute path, look in workingFolder
            if (!srcFile.exists) {
                try {
                    var basename = (new File(link.filePath)).name;
                    srcFile = new File(workingFolder + '\\' + basename);
                } catch (ignore2) {}
            }

            if (!srcFile.exists) {
                summary.skipped.push({
                    name:   linkName,
                    reason: 'file_not_found',
                    path:   String(link.filePath)
                });
                continue;
            }

            counter[0]++;
            var ext      = getExt(srcFile.name);
            var baseName = safeFilename('p' + pageLabel + '_' + counter[0] + '_' + srcFile.name.replace(/\.[^.]+$/, ''));
            var relPath  = outputSubDir + '\\' + baseName + ext;
            var destPath = workingFolder + '\\' + relPath;

            if (copyFileTo(srcFile, destPath)) {
                assets.push({
                    path: relPath,
                    data: { originalName: srcFile.name, page: pageLabel, type: 'linked' }
                });
                summary.extracted.push({ file: relPath, page: pageLabel, name: linkName });
            } else {
                summary.skipped.push({ name: linkName, reason: 'copy_failed' });
            }

        } catch (linkErr) {
            summary.skipped.push({ name: linkName, reason: 'error', detail: String(linkErr) });
        }
    }
}

// image extraction: pasted images (no link entry)

/**
 * Pasted images never appear in doc.links. They are accessed via
 * doc.allGraphics where graphic.itemLink === null.
 *
 * The only way to get them out is exportFile() to JPEG or PNG,
 * which causes some quality loss but is unavoidable for pasted images.
 */
function processPastedImages(doc, outFolder, outputSubDir, assets, summary, counter) {
    var graphics = doc.allGraphics;

    // Set JPEG export quality to maximum to minimise loss
    try {
        app.jpegExportPreferences.jpegRenderingStyle = JPEGOptionsFormat.PROGRESSIVE_ENCODING;
        app.jpegExportPreferences.jpegQuality        = JPEGOptionsQuality.MAXIMUM;
        app.jpegExportPreferences.resolution         = 300;
    } catch (prefErr) {
        // Non-fatal defaults are acceptable
    }

    for (var i = 0; i < graphics.length; i++) {
        var graphic = graphics[i];

        // itemLink == null means truly pasted (no file on disk)
        var hasPastedImage = false;
        try {
            hasPastedImage = (graphic.itemLink === null || graphic.itemLink === undefined);
        } catch (e) {
            continue; // can't determine — skip
        }

        if (!hasPastedImage) continue;

        var pageLbl = 'unknown';
        try { pageLbl = getPageLabel(graphic); } catch(ignore) {}

        counter[0]++;
        var baseName = safeFilename('pasted_p' + pageLbl + '_' + counter[0]);
        var relPath  = outputSubDir + '\\' + baseName + '.jpg';
        var destFile = new File(workingFolder + '\\' + relPath);

        try {
            graphic.exportFile(ExportFormat.JPG, destFile);

            if (destFile.exists) {
                assets.push({
                    path: relPath,
                    data: { originalName: baseName + '.jpg', page: pageLbl, type: 'pasted' }
                });
                summary.extracted.push({ file: relPath, page: pageLbl, name: baseName });
            } else {
                summary.skipped.push({ name: baseName, reason: 'export_produced_no_file' });
            }
        } catch (exportErr) {
            summary.skipped.push({ name: baseName, reason: 'export_failed', detail: String(exportErr) });
        }
    }
}

// main

function main() {
    try {
        var raw       = app.scriptArgs.get('parameters');
        var allParams = jsonParse(raw);

        workingFolder = allParams['workingFolder'];
        var params    = allParams['params'] || {};
        var targetDoc = params['targetDocument'] || 'document.indd';
        var docPath   = workingFolder + '\\' + targetDoc;

        var docFile = new File(docPath);
        if (!docFile.exists) {
            return buildFailure('DOC_NOT_FOUND', 'Document not found: ' + docPath);
        }

        var doc = app.open(docFile, false);

        var outputSubDir = 'extracted_images';
        var outFolder    = new Folder(workingFolder + '\\' + outputSubDir);
        if (!outFolder.exists) { outFolder.create(); }

        var assets  = [];
        var summary = { extracted: [], skipped: [] };

        // counter[0] is a shared mutable counter passed by reference via array
        var counter = [0];

        processLinkedAndEmbedded(doc, outFolder, outputSubDir, assets, summary, counter);
        processPastedImages(doc, outFolder, outputSubDir, assets, summary, counter);

        doc.close(SaveOptions.NO);
        return buildSuccess(assets, summary);

    } catch (e) {
        return buildFailure('SCRIPT_ERROR', String(e));
    }
}

main();
