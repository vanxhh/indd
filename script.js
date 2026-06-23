/**
 * extract_images/script.js
 * ExtendScript (ES3) – runs inside InDesign Server via Custom Scripts API
 *
 * What it does
 * ────────────
 * 1. Opens the target INDD document.
 * 2. Iterates every Link (placed image/asset) in the document.
 * 3. For each link whose source file exists locally (i.e. was supplied
 *    as an asset in the API request and placed in the workingFolder),
 *    copies the original file into the workingFolder output area.
 * 4. For links that are EMBEDDED (no external file – image data baked
 *    into the INDD), unembed them first, then copy the exported file.
 * 5. Returns the list of extracted files so the API uploads them.
 *
 * Input params (passed as JSON string via app.scriptArgs "parameters")
 * ─────────────────────────────────────────────────────────────────────
 *   targetDocument  : relative path to the .indd inside workingFolder
 *
 * Output (JSON string returned via main())
 * ─────────────────────────────────────────
 *   status           : "SUCCESS" | "FAILURE"
 *   assetsToBeUploaded : [{ path, data: { name, page, linkType } }]
 *   dataURL          : path to summary JSON (or "")
 */

// ─────────────────────────────────────────────────────────────────────────────
// Utility helpers
// ─────────────────────────────────────────────────────────────────────────────

var workingFolder = '';

function log(msg) {
    // Written to app std-out; visible in API job logs
    app.consoleout('[extract_images] ' + msg);
}

function writeJSON(obj, filename) {
    var f = new File(workingFolder + '/' + filename);
    f.encoding = 'UTF8';
    f.open('write');
    f.write(JSON.stringify(obj));
    f.close();
    return workingFolder + '/' + filename;
}

function copyFile(src, destPath) {
    // Read binary and write to dest
    var srcFile = new File(src);
    var destFile = new File(destPath);
    srcFile.copy(destFile);
    return destFile.exists;
}

function safeFilename(name) {
    // Replace characters that are illegal in filenames
    return name.replace(/[\/\\:*?"<>|]/g, '_');
}

function getExtension(filePath) {
    var parts = filePath.split('.');
    return parts.length > 1 ? '.' + parts[parts.length - 1].toLowerCase() : '';
}

function buildSuccessObj(assets, dataObj) {
    var obj = {};
    obj.status = 'SUCCESS';
    obj.assetsToBeUploaded = assets;
    obj.dataURL = dataObj ? writeJSON(dataObj, 'extraction_summary.json') : '';
    return JSON.stringify(obj);
}

function buildFailureObj(code, msg) {
    return JSON.stringify({ status: 'FAILURE', errorCode: code, errorString: msg });
}

// ─────────────────────────────────────────────────────────────────────────────
// Page number helper (works on InDesign Server DOM)
// ─────────────────────────────────────────────────────────────────────────────

function getPageNumber(link) {
    try {
        var parent = link.parent;
        // Walk up the DOM to find a Page
        while (parent && !(parent instanceof Page)) {
            parent = parent.parent;
        }
        if (parent instanceof Page) {
            return parent.name; // page label / number as string
        }
    } catch (e) {}
    return 'unknown';
}

// ─────────────────────────────────────────────────────────────────────────────
// Main
// ─────────────────────────────────────────────────────────────────────────────

function main() {
    try {
        // Parse parameters injected by the Custom Scripts API
        var rawParams = app.scriptArgs.get('parameters');
        var allParams = JSON.parse(rawParams);

        workingFolder = allParams['workingFolder'];
        var params    = allParams['params'] || {};
        var targetDoc = params['targetDocument'] || 'document.indd';

        var docPath = workingFolder + '/' + targetDoc;
        log('Opening document: ' + docPath);

        var docFile = new File(docPath);
        if (!docFile.exists) {
            return buildFailureObj('DOC_NOT_FOUND', 'Document not found: ' + docPath);
        }

        var doc = app.open(docFile, false); // false = do not add to recent files

        var links   = doc.links;
        var assets  = [];
        var summary = { extracted: [], skipped: [] };
        var outputDir = workingFolder + '/extracted_images';

        // Create output sub-directory
        var outFolder = new Folder(outputDir);
        if (!outFolder.exists) { outFolder.create(); }

        log('Found ' + links.length + ' link(s) in document.');

        for (var i = 0; i < links.length; i++) {
            var link = links[i];
            var linkName = link.name;
            var page     = getPageNumber(link);
            var linkType = 'linked';

            try {
                // ── Case 1: embedded image (no external file) ──────────────
                if (link.status === LinkStatus.LINK_EMBEDDED) {
                    linkType = 'embedded';
                    log('Unembedding: ' + linkName);
                    // unembed() places the asset back in the workingFolder
                    link.unembed(new Folder(outputDir), false);
                    // After unembed the link now points to a real file
                    link = doc.links.item(i); // re-fetch (index may shift)
                }

                // ── Case 2: linked but file is in workingFolder ────────────
                var srcFilePath = link.filePath;
                var srcFile     = new File(srcFilePath);

                if (!srcFile.exists) {
                    // The linked asset may have been supplied as an API asset
                    // and placed relative to workingFolder
                    var altPath = workingFolder + '/' + new File(srcFilePath).name;
                    srcFile = new File(altPath);
                }

                if (srcFile.exists) {
                    var ext      = getExtension(srcFile.name);
                    var baseName = safeFilename(
                        'image_p' + page + '_' + (i + 1) + '_' + srcFile.name.replace(/\.[^.]+$/, '')
                    );
                    var destPath = outputDir + '/' + baseName + ext;

                    if (copyFile(srcFile.fsName, destPath)) {
                        var relPath = 'extracted_images/' + baseName + ext;
                        assets.push({
                            path: relPath,
                            data: {
                                name:     linkName,
                                page:     page,
                                linkType: linkType,
                                original: srcFile.name
                            }
                        });
                        summary.extracted.push({ file: relPath, page: page, original: linkName });
                        log('Extracted: ' + relPath);
                    } else {
                        summary.skipped.push({ name: linkName, reason: 'copy_failed' });
                        log('WARN: copy failed for ' + linkName);
                    }
                } else {
                    summary.skipped.push({ name: linkName, reason: 'file_not_found', path: srcFilePath });
                    log('WARN: source file not found for ' + linkName + ' at ' + srcFilePath);
                }

            } catch (linkErr) {
                summary.skipped.push({ name: linkName, reason: 'error', detail: linkErr.toString() });
                log('ERROR processing link ' + linkName + ': ' + linkErr);
            }
        }

        doc.close(SaveOptions.NO);
        log('Done. Extracted: ' + assets.length + ', Skipped: ' + summary.skipped.length);

        return buildSuccessObj(assets, summary);

    } catch (e) {
        return buildFailureObj('SCRIPT_ERROR', e.toString());
    }
}

// Entry point – Custom Scripts API calls main()
main();
