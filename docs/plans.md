# SlicerLNQ

## Overall Plan

I want the parent directory to be the git repo for a custom 3D Slicer Extension for the LNQ project (see lnqproject.org for overview).

This Extention should have a central module called LNQStudo in the LNQ category.  This will pull to gether functionality that has been prootyped in the LNQ-data file share across various scripts and conventions.  The goal is to systematize several features so that it's easier to perform experiments and expand the data and training methods and test them on various data cohorts.

## Module Structure for LNQStudio

The general setup of the module will be to have a tabbed widget layout, with some data selection widgets at the top that apply to all the tabs below.

General structure of tabs:
* Config: settings like toplevel directory and database to be used for cohorts and result info
* Cohorts: to triage patients based on inclusion and exclusion criteria.  Should result in automatic consort diagram.
* Protocols: to define the segmentation to be performed (i.e. by providing a color table with defined terminology)
* Annotate: use 3D Slicer tools to create or edit segmentations
* Train: use annotated data to train segmentation models
* Infer: apply trained models to selected cohorts
* Review: specialied tools to iterate through inference results
* Deploy: package up trained models for external application and review
* Dashboard: show current training and inference status, plus internal and external review results

### Top level data selection

* Config: basically top level directory select, but also possibly target compute system for training and inference commands (could be Jetstream2 via openstack or vast.ai via its cli or something else like the Martinos cluster)

* Cohorts: It should be possible to pick source datasets that can be broad and narrow them down to specfic targets.  For example the source could be all of IDC or a directory of TIMC export data, and the exclusion criteria could related to primary cancer site, CT quality, body parts included in scan, etc.  The result is a set of pointers to data and metadata about how they were selected.  Effectively the information in a Consort diagram for a medical research study.

    * Cohort Select Widget: this is to select the cohort of patient scans that the tabs will be used by all the tabs below where a cohort is needed.  A cohort will be defined using the Cohorts tab.  Cohort information will be saved in an datbase.  Cohorts would be defined by URLs, such as DICOMWeb endpoints or local file URIs.  This module should also allow selection of csv or jsonl files with info per-scan so that we filter scans by things like scan type or primary disease.


* Protocols: Define and manage anotation protocols.  These should include both the color table with controled terminology along with any instructions needed to define the SOP to be used when mapping that terminology to specific scan.  This would include the decision making guidelines for ambiguous anatomical boundaries, or how to apply segmentation ruls in the context of pathological morphological alterations.  

    * Prootcol Select Widget: this is to select the protocol being used  by all the tabs below where a protocol is needed.

* Projects: This level should combine a Cohort with a Protocol along with user roles including Admin to manage project, Annotators for people who will perform the work, Reviewers for people who will review the work.

* Annotate: this should allow an expert annotator to iterate through the cohort defining ground truth segmentation. It should also have features to annotators to add notes, such as status being completed, scan being bad, segmentation needing input from another expert, etc.  It should also have the ability to do background prefetch of the next scan.  It should also have a way to do active learning, where information from the inference or Review or Deploy steps can be used to prioritize scans for annotation.  It should also be possible to use the inference results from a previous generation of trained model and add scans to the training set after cleaning up the segmentations from the previous inference run.  All the metadata about who did the segmentation, what software was used, what hardware was used, etc should be saved with the segmentations. When a user logs in they can be presented with their TODO list of projects where they are assigned to annotate.

* Train: Once we have a set of annotations for a cohort, whether it completes a project or is just running an intermediate test, we can use this tab to trigger a training job. I'd like the training to be something we launch on a cloud resource can monitor periodically on the dashboard.  I would like to be able to look at the evolution of the training as it goes by loading validation data from the held out scans for each fold as volumes and segmentations in Slicer so I get a visual sense of how the model is converging.  It would be good for the training process to store the incemental segmentations for the validation cases so that they can be loaded as sequences in Slicer for efficient review.  The result of the training should be a trained model generation (analogous to retro6 being the sixth generation of the retro model).  I want all the provenance of each model and each generation (input data, software versions, hardware platforms, user who invoked the training, etc) to be preserved in either schema'd json files or the sqlite database for easy access and evaluation.

* Infer: This tab should be usable to apply any model generation to any cohort or subcohort or single scan to create an inference, which is the segmentation results along with the metadata about which model generation was used, on which machine, which which software versions were used, etc.

* Review: This should be a dedicated interface that supports overlay of inference on the data they were inferred on.  Multiple inferences of the same cohort should be possible to compare in different visualization modes, based on the LNQ retro series of scripts, so that users can easily see the different generations of the model applied to different cohorts.

* Deploy: This module should allow exporting a cohort plus a inference to a web based review installation, such as a Js2 or google/aws bucket.  The deloyed data should be in DICOM, including SEG with embedded metadata.  The deployment can use a cusomized version of the OHIF Viewer to display.  The web app that gets deployed with the inference should allow users to make signed comments on the segmentation by having them login to a trusted authentication provider, such as ORCID or ACCESS and get a signing key that is securely stored in their browser and used to cyrptographically sign documents that encode their commentary on the segments.  Commentary could be simply thumbs up/down on the quality of the result, a 5-point Likert scale respone on clinical utility, or specific overcall/undercall marks at specific locations in the scan.  The deployment can be coupled with a lightweight server that handles the DICOMweb API and also accepts back the signed commentary in the form of DICOM Structured Reports that reference the SEG that is being commented on.

* Dashboard: This tab should be summary of what's going on in all the other tabs, so counts plus drill down of available data sources, cohorts, annotations, training jobs, models and model generations plus inference sets and deployments.

## Architectural considerations

* Should we require all intermediate and output data files to be in DICOM, when an appropriate container exists?  And should we put everything into DICOMweb storage.  And should we standardize on a particular cloud instantiation or make it more general?  Specifically, the current LNQ scripts are very implicit and joined together by code and context.  The DICOM alternative would be to make everything explicit by having metadata in, for example, the SEG objects contain references to their sources and context data about who did the segmentation as part of which protocol.  In terms of cloud-specificity, we could use BigQuery instead of a disk-based sqlite database and we could use GCP healthcare DICOM stores for the original and derived data.  A downside of Google is that it would cost money and be sort-off locked in. On the other hand it would mean there's a clean and well documented path to handling security, explicity access control, and other benefits.

* GUI implementation.  If all the information defining the overall process is stored in web accessible resources, does it make sense to have a web GUI over a Qt based one?  Perhaps the dashboard should be the toplevel interface and each user how logs in should be presented with a TODO list of sorts, where their annotation or review projects are presented with a completion status indicator (e.g. 12 of 100 cases annotated).  Also the dashboard should report the status or any training or inference jobs in progress.  It makes some sense to have this interface consistent within or outside of Slicer.

* How much should we generalize this architecture to work for non-LNQ projects.  Our main priority is to solve LNQ requirements, but it's tempting to leave this general enough to handle other lesions or other segmentation tasks.

* It would be preferable for the whole dashboard interface to be serverless, so that the full user interface can be populated by DICOMweb or BQ queries.

* For Train and Infer jobs, for practical reasons I want to do these on Jetstream2 instances, meaning that they would only be launched from local machine when the page is being run inside of Slicer and where the local machine is configured with a clouds.yaml with keys to run compute instances.  Once launched, the runner script for the training should be piping the logs to Js2 buckets that can be inspected by the dashboard and processed into graphs or other summary views for quick consumption or drilldown via the dashboard.

* How shall we represent the key data elements of this design?  I'm thinking an SQL database schema for tables like Cohorts, Protocols, Projects, etc would be a clean option.  Another could be a json schema for each of those concepts and a noSQL database like couchdb to implement the relations, like patientsByCohort or cohortsByPatient etc.  I still like the couchdb philosophy but it's not clear what the 2026 implementation options for a couch-like managed database are like especially within the GCP ecosystem.

* Should we design this for scalability, so it's more like Crowds Cure Cancer, or for project specific efficiency, like the existing lnq scripts.