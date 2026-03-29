# statschema — lines of code

Generated: 2026-03-28 16:07 UTC  
Commit: `b76e9dc`


---

## Grand total

Layer            | Files |   Code | Comment | Blank |  Total | Note                                                                            
---------------- | ----: | -----: | ------: | ----: | -----: | --------------------------------------------------------------------------------
  src/interface  |     2 |    979 |      35 |   159 |  1_173 | CLI / API entry points                                                          
  src/core       |    56 |  6_624 |     492 | 1_256 |  8_372 | stable domain logic — grows with features                                       
  src/generators |     5 |  1_143 |     161 |   251 |  1_555 | grows with each new generator backend                                           
  src/locale     |     5 |    544 |     112 |   156 |    812 | locale patterns + inference (grows per language)                                
  src/dialects   |    34 |  3_638 |     245 |   662 |  4_545 | grows with each new database added                                              
platform         |     7 |    558 |     342 |   117 |  1_017 | Lima/Podman/QEMU configs + Databricks Connect scripts (grows per cloud platform)
benchmarks       |    21 |  5_914 |     614 | 1_051 |  7_579 |                                                                                 
  tests/core     |    19 | 11_862 |     845 | 2_090 | 14_797 | unit + offline integration                                                      
  tests/dialect  |    15 |  5_559 |     435 | 1_100 |  7_094 | live DB engine coverage                                                         
  tests/app      |     6 |  2_417 |     167 |   470 |  3_054 | real-world application schemas                                                  
**TOTAL**        |   170 | 39_238 |   3_448 | 7_312 | 49_998 |                                                                                 

### Dialect breakdown (src/statschema)

Dialect             | Files |  Code | Total
------------------- | ----: | ----: | ----:
dialects/databricks |     3 |   144 |   172
dialects/db2        |     5 |   419 |   493
dialects/mysql      |     5 |   290 |   346
dialects/oracle     |     4 |   218 |   261
dialects/postgres   |     5 |   290 |   351
dialects/shared     |     7 | 2_056 | 2_652
dialects/sqlserver  |     5 |   221 |   270
interface           |     2 |   979 | 1_173
**dialect total**   |    36 | 4_617 | 5_718

### Generator backend breakdown (src/statschema)

Generator backend   | Files |  Code | Total
------------------- | ----: | ----: | ----:
generators/pandas   |     1 |   567 |   766
generators/spark    |     4 |   576 |   789
**generator total** |     5 | 1_143 | 1_555

---

## `src/statschema`

Group               | Files |   Code | Comment | Blank |  Total
------------------- | ----: | -----: | ------: | ----: | -----:
interface           |     2 |    979 |      35 |   159 |  1_173
src root            |    29 |  1_737 |     114 |   288 |  2_139
core                |    15 |  3_280 |     289 |   659 |  4_228
query               |     6 |  1_160 |      81 |   234 |  1_475
services            |     6 |    447 |       8 |    75 |    530
locale              |     5 |    544 |     112 |   156 |    812
dialects/shared     |     7 |  2_056 |     241 |   355 |  2_652
dialects/databricks |     3 |    144 |       0 |    28 |    172
dialects/db2        |     5 |    419 |       2 |    72 |    493
dialects/mysql      |     5 |    290 |       0 |    56 |    346
dialects/oracle     |     4 |    218 |       0 |    43 |    261
dialects/postgres   |     5 |    290 |       2 |    59 |    351
dialects/sqlserver  |     5 |    221 |       0 |    49 |    270
generators/pandas   |     1 |    567 |      74 |   125 |    766
generators/spark    |     4 |    576 |      87 |   126 |    789
**TOTAL**           |   102 | 12_928 |   1_045 | 2_484 | 16_457

### Per-file detail (≥ 50 total lines)

File                            | Code | Comment | Blank | Total
------------------------------- | ---: | ------: | ----: | ----:
__init__.py                     |  250 |      14 |    16 |   280
cli.py                          |  971 |      33 |   156 | 1_160
core/builtin_generators.py      |  292 |      30 |    67 |   389
core/dialect_registry.py        |   87 |       9 |    16 |   112
core/generator.py               |  134 |      12 |    36 |   182
core/loader.py                  |  185 |       2 |    25 |   212
core/model.py                   |  578 |      68 |   112 |   758
core/override_model.py          |  329 |      30 |    58 |   417
core/pandas_builder.py          |  567 |      74 |   125 |   766
core/pipeline_parser.py         |   44 |       1 |     9 |    54
core/postgen.py                 |   87 |       4 |    22 |   113
core/row_generator.py           |  340 |      50 |    72 |   462
core/schema_io.py               |  243 |      22 |    51 |   316
core/sdv_parser.py              |  127 |       2 |    24 |   153
core/semantic_hints.py          |  341 |      28 |    91 |   460
core/stats_io.py                |  378 |      31 |    73 |   482
core/stats_model.py             |  339 |      27 |    72 |   438
core/ydata_parser.py            |  116 |       1 |    22 |   139
data_loader.py                  |  221 |      16 |    29 |   266
db_stats_collector.py           |  209 |      10 |    48 |   267
ddl_emitter.py                  |  184 |      14 |    41 |   239
ddl_parser.py                   |   42 |       4 |     5 |    51
dialects/_collector_shared.py   |  853 |      84 |   114 | 1_051
dialects/_emitter_shared.py     |  199 |      14 |    46 |   259
dialects/_loader_shared.py      |  282 |      15 |    54 |   351
dialects/_parser_shared.py      |  625 |     127 |   110 |   862
dialects/base.py                |   83 |       0 |    26 |   109
dialects/databricks/injector.py |   98 |       0 |    14 |   112
dialects/db2/collector.py       |   58 |       0 |    12 |    70
dialects/db2/injector.py        |  139 |       0 |    23 |   162
dialects/db2/loader.py          |  175 |       2 |    23 |   200
dialects/mysql/collector.py     |   58 |       0 |    12 |    70
dialects/mysql/injector.py      |  148 |       0 |    22 |   170
dialects/oracle/collector.py    |   58 |       0 |    12 |    70
dialects/oracle/emitter.py      |   44 |       0 |     6 |    50
dialects/oracle/injector.py     |  104 |       0 |    17 |   121
dialects/postgres/collector.py  |   58 |       0 |    12 |    70
dialects/postgres/injector.py   |  156 |       2 |    25 |   183
dialects/sqlserver/collector.py |   58 |       0 |    12 |    70
dialects/sqlserver/injector.py  |   61 |       0 |    14 |    75
dialects/sqlserver/loader.py    |   49 |       0 |     9 |    58
patterns/de_DE.comments.yaml    |   50 |      20 |    16 |    86
patterns/de_DE.yaml             |   53 |      35 |    17 |   105
patterns/en_US.comments.yaml    |   50 |      14 |    16 |    80
patterns/en_US.yaml             |   50 |      15 |    16 |    81
query/analyzer.py               |  447 |      42 |    92 |   581
query/collector.py              |  311 |      28 |    45 |   384
query/model.py                  |  123 |       3 |    39 |   165
query/replayer.py               |  150 |       3 |    35 |   188
query/transpiler.py             |  128 |       5 |    23 |   156
services/benchmark.py           |   94 |       2 |    12 |   108
services/collect.py             |   84 |       0 |    12 |    96
services/generate.py            |   69 |       3 |    14 |    86
services/inject.py              |   78 |       3 |    13 |    94
services/transpile.py           |  100 |       0 |    21 |   121
spark/dbldatagen_builder.py     |  346 |      67 |    60 |   473
spark/generator.py              |   87 |       0 |    21 |   108
spark/schema_transforms.py      |  142 |      20 |    45 |   207
stats_injector.py               |   92 |       2 |    19 |   113
v1_bridge.py                    |  395 |      54 |    57 |   506

---

## `platform/` (Lima · Podman · Databricks Connect)

Each new target platform (AWS, GCloud, Azure) adds VM/container config files
and startup scripts here — isolated from benchmark and test logic.

File                       | Code | Comment | Blank | Total
-------------------------- | ---: | ------: | ----: | ----:
_common.sh                 |  178 |      68 |    27 |   273
config/lima/db2.yaml       |   68 |      62 |    14 |   144
config/lima/oracle.yaml    |   46 |      51 |    11 |   108
config/lima/sqlserver.yaml |   38 |      33 |     9 |    80
run_lakebase_target.sh     |   49 |      35 |    11 |    95
scripts/lakebase-down.sh   |   49 |      34 |    15 |    98
scripts/lakebase-up.sh     |  130 |      59 |    30 |   219
**TOTAL**                  |  558 |     342 |   117 | 1_017

---

## `benchmarks/`

### Python (pipeline / orchestration)

File                      |  Code | Comment | Blank | Total
------------------------- | ----: | ------: | ----: | ----:
bench_config.py           |   156 |      18 |    36 |   210
build_row_count_report.py |   260 |      22 |    49 |   331
check_run.py              |   299 |      26 |    62 |   387
identity_test.py          | 1_462 |     160 |   191 | 1_813
run_all_bench.py          |   252 |      21 |    42 |   315
run_bench.py              |   464 |      45 |    83 |   592
run_matrix.py             |   523 |      55 |    96 |   674
run_strategy_bench.py     |   238 |      30 |    65 |   333
run_tpcb_bench.py         |   284 |      15 |    48 |   347
tpc_generators.py         |   458 |      19 |    79 |   556
tpc_schemas.py            |   312 |      11 |    34 |   357
**total**                 | 4_708 |     422 |   785 | 5_915

### Python (dialect adapters — `benchmarks/dialects/`)

File                  | Code | Comment | Blank | Total
--------------------- | ---: | ------: | ----: | ----:
dialects/__init__.py  |   23 |       1 |     3 |    27
dialects/_base.py     |   44 |      15 |    18 |    77
dialects/db2.py       |  176 |      19 |    32 |   227
dialects/mysql.py     |   85 |      18 |    29 |   132
dialects/oracle.py    |  118 |      15 |    28 |   161
dialects/postgres.py  |  172 |      18 |    33 |   223
dialects/sqlserver.py |  111 |      23 |    23 |   157
**total**             |  729 |     109 |   166 | 1_004

### Shell scripts (runners)

File            | Code | Comment | Blank | Total
--------------- | ---: | ------: | ----: | ----:
run_bench.sh    |   29 |      19 |     9 |    57
run_identity.sh |   45 |      30 |     9 |    84
**total**       |   74 |      49 |    18 |   141

### Scripts / tools (`scripts/`)

File                  | Code | Comment | Blank | Total
--------------------- | ---: | ------: | ----: | ----:
scripts/loc_report.py |  403 |      34 |    82 |   519
**total**             |  403 |      34 |    82 |   519


---

## `tests/`

### Core tests (unit / offline integration)

File                          |   Code | Comment | Blank |  Total
----------------------------- | -----: | ------: | ----: | -----:
__init__.py                   |      0 |       1 |     0 |      1
conftest.py                   |     67 |      21 |    35 |    123
helpers.py                    |    146 |       6 |    30 |    182
live_helpers.py               |    310 |      11 |    43 |    364
test_canonical_generation.py  |    388 |      28 |    67 |    483
test_cli.py                   |    340 |      28 |    70 |    438
test_coverage_gaps.py         |  1_547 |      92 |   300 |  1_939
test_data_loader.py           |    406 |      39 |   115 |    560
test_ddl_roundtrip.py         |  2_903 |     195 |   409 |  3_507
test_loader_dtype_coverage.py |    955 |      87 |   207 |  1_249
test_migration_roundtrip.py   |    768 |      42 |   121 |    931
test_pandas_builder.py        |    296 |      34 |    67 |    397
test_query_workload.py        |    253 |      15 |    40 |    308
test_schema_parser.py         |  1_492 |      51 |   207 |  1_750
test_semantic_hints.py        |    475 |      64 |    94 |    633
test_table_instances.py       |    253 |      25 |    59 |    337
test_tpcds_workload.py        |    112 |      16 |    30 |    158
test_tpce_workload.py         |    253 |      31 |    41 |    325
test_v1_bridge.py             |    898 |      59 |   155 |  1_112
**core total**                | 11_862 |     845 | 2_090 | 14_797

### Dialect tests (live DB engine coverage)

File                          |  Code | Comment | Blank | Total
----------------------------- | ----: | ------: | ----: | ----:
test_live_cockroachdb.py      |   378 |      25 |    64 |   467
test_live_db2.py              |   330 |      22 |    61 |   413
test_live_distribution.py     |   211 |      22 |    56 |   289
test_live_identity.py         |   238 |      19 |    63 |   320
test_live_mariadb.py          |   361 |      22 |    63 |   446
test_live_mysql.py            |   364 |      22 |    59 |   445
test_live_neon.py             |   172 |       5 |    34 |   211
test_live_oracle.py           |   436 |      29 |    72 |   537
test_live_pg.py               |   375 |      21 |    63 |   459
test_live_roundtrip.py        |   453 |      52 |    95 |   600
test_live_sqlserver.py        |   337 |      24 |    59 |   420
test_live_stats_minmax.py     |   329 |      23 |    65 |   417
test_live_stats_transpiler.py |   852 |      70 |   196 | 1_118
test_live_synth.py            |   572 |      51 |   110 |   733
test_live_type_coverage.py    |   151 |      28 |    40 |   219
**dialect total**             | 5_559 |     435 | 1_100 | 7_094

### App tests (real-world application schemas)

File                        |  Code | Comment | Blank | Total
--------------------------- | ----: | ------: | ----: | ----:
test_live_adventureworks.py |   611 |      22 |   107 |   740
test_live_chinook.py        |   311 |      25 |    66 |   402
test_live_gitea.py          |   330 |      26 |    67 |   423
test_live_lakebase.py       |   424 |      28 |    79 |   531
test_live_mautic.py         |   402 |      36 |    77 |   515
test_live_oracle_hr.py      |   339 |      30 |    74 |   443
**app total**               | 2_417 |     167 |   470 | 3_054

### All tests total

Category  | Files |   Code |  Total
--------- | ----: | -----: | -----:
core      |    19 | 11_862 | 14_797
dialect   |    15 |  5_559 |  7_094
app       |     6 |  2_417 |  3_054
**TOTAL** |    40 | 19_838 | 24_945
