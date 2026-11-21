CREATE TABLE IF NOT EXISTS publication (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, year INTEGER, doi TEXT, authors JSON, journal TEXT,
 corpus_status TEXT NOT NULL DEFAULT 'needs_review' CHECK(corpus_status IN ('included','excluded_tandem','excluded_theoretical','needs_review')),
 corpus_reason TEXT, corpus_manual INTEGER NOT NULL DEFAULT 0, is_fixture INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pdf_version (
 id TEXT PRIMARY KEY, publication_id TEXT NOT NULL REFERENCES publication(id), sha256 TEXT NOT NULL UNIQUE, filename TEXT NOT NULL,
 pages JSON NOT NULL DEFAULT '[]', processing_status TEXT NOT NULL CHECK(processing_status IN ('queued','processing','completed','partial','failed')),
 error_message TEXT, failed_pages JSON NOT NULL DEFAULT '[]', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 pv_extraction_enabled INTEGER CHECK(pv_extraction_enabled IN (0,1))
);
CREATE TABLE IF NOT EXISTS material (
 id TEXT PRIMARY KEY, publication_id TEXT NOT NULL REFERENCES publication(id), material_name_raw TEXT, formula_normalized TEXT,
 material_class TEXT, dimensionality TEXT, additives JSON, bandgap_value REAL, bandgap_unit TEXT, bandgap_method TEXT
);
CREATE TABLE IF NOT EXISTS device (
 id TEXT PRIMARY KEY, publication_id TEXT NOT NULL REFERENCES publication(id), material_id TEXT REFERENCES material(id),
 device_label TEXT NOT NULL, device_role TEXT, device_type TEXT, architecture TEXT, substrate_type TEXT, active_area REAL,
 area_unit TEXT, area_definition TEXT, encapsulation TEXT
);
CREATE TABLE IF NOT EXISTS device_layer (
 id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES device(id), layer_order INTEGER, layer_function TEXT,
 layer_material TEXT, material_id TEXT REFERENCES material(id), layer_thickness REAL, thickness_unit TEXT,
 deposition_method TEXT, annealing_temperature TEXT, annealing_time TEXT, atmosphere TEXT
);
CREATE TABLE IF NOT EXISTS measurement (
 id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES device(id), measurement_type TEXT, spectrum TEXT,
 light_intensity REAL, light_intensity_unit TEXT, intensity_source_id TEXT REFERENCES source_reference(id),
 scan_direction TEXT, scan_rate TEXT, mask_used INTEGER, certified INTEGER, temperature TEXT, stability_protocol TEXT,
 stress_conditions TEXT, batch_label TEXT, condition_label TEXT
);
CREATE TABLE IF NOT EXISTS measurement_result (
 id TEXT PRIMARY KEY, publication_id TEXT NOT NULL REFERENCES publication(id), pdf_version_id TEXT NOT NULL REFERENCES pdf_version(id),
 device_id TEXT REFERENCES device(id), measurement_id TEXT REFERENCES measurement(id), material_id TEXT REFERENCES material(id),
 candidate_device_ids JSON NOT NULL DEFAULT '[]', candidate_measurement_ids JSON NOT NULL DEFAULT '[]',
 metric_name TEXT NOT NULL, metric_value REAL, metric_unit TEXT, aggregation TEXT NOT NULL DEFAULT 'unknown', spread_value REAL,
 n_devices INTEGER, time_point REAL, time_unit TEXT, range_min REAL, range_max REAL, inequality_op TEXT,
 value_state TEXT NOT NULL CHECK(value_state IN ('numeric','numeric_zero','missing','ambiguous','unparseable')),
 raw_value TEXT, raw_unit TEXT, raw_record JSON NOT NULL, normalized_value REAL, normalized_unit TEXT,
 result_origin TEXT NOT NULL DEFAULT 'primary' CHECK(result_origin IN ('primary','cited')), cited_publication_id TEXT REFERENCES publication(id),
 acquisition_method TEXT NOT NULL DEFAULT 'published_numeric' CHECK(acquisition_method IN ('published_numeric','digitized_plot','calculated','assumption')),
 extraction_method TEXT, confidence REAL, formula TEXT, input_result_ids JSON, calculation_inputs JSON, assumption_basis TEXT,
 extraction_status TEXT NOT NULL CHECK(extraction_status IN ('Извлечено автоматически','Подтверждено пользователем','Требует проверки')),
 duplicate_of TEXT REFERENCES measurement_result(id), note TEXT,
 manual INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS source_reference (
 id TEXT PRIMARY KEY, publication_id TEXT NOT NULL REFERENCES publication(id), pdf_version_id TEXT NOT NULL REFERENCES pdf_version(id),
 source_page INTEGER NOT NULL, source_section TEXT, source_table TEXT, source_figure TEXT, source_fragment TEXT,
 bbox JSON NOT NULL, source_type TEXT NOT NULL, fragment_role TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS result_source (
 result_id TEXT NOT NULL REFERENCES measurement_result(id), source_reference_id TEXT NOT NULL REFERENCES source_reference(id),
 PRIMARY KEY (result_id, source_reference_id)
);
CREATE TABLE IF NOT EXISTS plot_dataset (
 id TEXT PRIMARY KEY, publication_id TEXT NOT NULL REFERENCES publication(id), pdf_version_id TEXT NOT NULL REFERENCES pdf_version(id),
 source_page INTEGER NOT NULL, source_figure TEXT, panel_id TEXT, plot_type TEXT NOT NULL, plot_bbox JSON NOT NULL,
 caption_text TEXT, legend_text TEXT, crop_image TEXT, axes JSON NOT NULL, points JSON NOT NULL, digitization_method TEXT,
 stroke_width_pdf REAL, uncertainty JSON, uncertainty_method TEXT NOT NULL, is_approximate INTEGER NOT NULL DEFAULT 1 CHECK(is_approximate=1),
 acquisition_method TEXT NOT NULL DEFAULT 'digitized_plot', device_id TEXT REFERENCES device(id),
 measurement_id TEXT REFERENCES measurement(id), series_label TEXT, manual INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS plot_source (
 plot_id TEXT NOT NULL REFERENCES plot_dataset(id), source_reference_id TEXT NOT NULL REFERENCES source_reference(id),
 PRIMARY KEY (plot_id, source_reference_id)
);
CREATE TABLE IF NOT EXISTS validation_issue (
 id TEXT PRIMARY KEY, publication_id TEXT NOT NULL REFERENCES publication(id), rule_code TEXT NOT NULL, severity TEXT NOT NULL,
 compared_values JSON NOT NULL, threshold JSON, description TEXT NOT NULL, hypothesis TEXT,
 resolution_status TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS issue_result (
 issue_id TEXT NOT NULL REFERENCES validation_issue(id), result_id TEXT NOT NULL REFERENCES measurement_result(id),
 PRIMARY KEY(issue_id,result_id)
);
CREATE TABLE IF NOT EXISTS issue_source (
 issue_id TEXT NOT NULL REFERENCES validation_issue(id), source_reference_id TEXT NOT NULL REFERENCES source_reference(id),
 PRIMARY KEY(issue_id,source_reference_id)
);
CREATE TABLE IF NOT EXISTS audit_log (
 id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, field_name TEXT NOT NULL,
 old_value JSON, new_value JSON, author TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TRIGGER IF NOT EXISTS immutable_result_source BEFORE UPDATE OF raw_value,raw_unit,raw_record ON measurement_result
WHEN OLD.raw_value IS NOT NEW.raw_value OR OLD.raw_unit IS NOT NEW.raw_unit OR OLD.raw_record IS NOT NEW.raw_record
BEGIN SELECT RAISE(ABORT, 'Published values are immutable'); END;
CREATE INDEX IF NOT EXISTS results_publication ON measurement_result(publication_id, measurement_id);
CREATE INDEX IF NOT EXISTS versions_publication ON pdf_version(publication_id);
CREATE TABLE IF NOT EXISTS result_plot (
 result_id TEXT NOT NULL REFERENCES measurement_result(id), plot_id TEXT NOT NULL REFERENCES plot_dataset(id),
 PRIMARY KEY(result_id,plot_id)
);
