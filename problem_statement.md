# Assumptions

databases run benchmarks like TPC-X to measure their performance.
each database is designed and  optimized for certain workloads.
in all cases, the queries performance depends on optimizer which in turn depends on the statistics.  
the physical hardware performance also play a key factor we will assume to equivalent here.

DBA and app developers often look at benchmark results.
they are matching the app and data to benchmark.
assuming there is compelling business case the the database, there is evaluation cycle.

in most cases, the customer would try to replicate the vendor's benchmark inhousr to  learn about the database.
then th there are two path for existing application.
the most expensive is to port the applicatio and move the data to the new database.
the 2nd option is to build simulator and move the data to the new database.
the 3rd option is to build simulator and build syhtnetic data in the new database.

most often, 3rd option will be chooseni.
simulator are synthetic data are often maintained to troubleshooiting existing applcation and existing database.

evaluating a new database often involve a new simualtor and new synthetic data.
then how can be simpify simulator building and synthetic data creation.
simulator can be build easily by capturing the existing queries databases.
since the optimizer looks at stat, a novel idea is to build the synthen data to match existing production database's stats.
when database performance is poor, the first question is always are the stats accruate.  
if the statistics are the same the optimizer will build the same exeution path and the performance should be the same.

# will this work

tpc.org has tpc-b,c,e,h, di,ds designed to simulate real world database application.
if we design a test each tpc-x data is loaded, stats collect, use the stats to build syhtne data on the target, the evaluation the accuracy of the optimizer by the target database, then we can shorten the database evaluation cycrle.  it can also improve the sythn data generatorn based on stats.
for tpc-x, we know the queiries, but in real world, we need to collect the top queries targeting the schema or tables we want.  the cptured queries will enable us to compare optimize plan accruacy and data accruacy more accrucately.

# methodology

talk about idendity methodlogd, stats collected above the standard stats, describe live database and actual testing with all tpc-x.

# results

share idendity results
share lakebase target results

# conclustion

statschema show promise.  describe where it works and where it does not.
let me know what you think so that we can imprive the tooling. 


