from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Union, List, Tuple, Dict, Optional, Type, Any
from types import SimpleNamespace

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
else:
    S3Client = object

from dataplug.util import split_s3_path, head_object, load_attributes
from dataplug.storage import PureS3Path, PickleableS3ClientProxy
from dataplug.preprocess import (
    BatchPreprocessor,
    MapReducePreprocessor,
    PreprocessorBackendBase,
)
from dataplug.dataslice import CloudObjectSlice

logger = logging.getLogger(__name__)


class CloudDataType:
    def __init__(
        self,
        preprocessor: Union[Type[BatchPreprocessor], Type[MapReducePreprocessor]] = None,
        inherit_from: Type["CloudDataType"] = None,
    ):
        self.co_class: object = None
        self.__preprocessor: Union[Type[BatchPreprocessor], Type[MapReducePreprocessor]] = preprocessor
        self.__parent: "CloudDataType" = inherit_from

    @property
    def preprocessor(
        self,
    ) -> Union[Type[BatchPreprocessor], Type[MapReducePreprocessor]]:
        if self.__preprocessor is not None:
            return self.__preprocessor
        elif self.__parent is not None:
            return self.__parent.preprocessor
        else:
            raise Exception("There is not preprocessor")

    def __call__(self, cls):
        if not inspect.isclass(cls):
            raise TypeError(f"CloudObject expected to use with class type, not {type(cls)}")

        if self.co_class is None:
            self.co_class = cls
        else:
            raise Exception(f"Can't overwrite decorator, now is {self.co_class}")

        return self


class CloudObject:
    def __init__(
        self,
        data_type: CloudDataType,
        s3_uri_path: str,
        s3_config: dict = None,
    ):
        """
        Create a reference to a Cloud Object
        :param data_type: Specify the Cloud data type for this object
        :param s3_uri_path: Full S3 uri in form s3://bucket/key for this object
        :param s3_config: Extra S3 config
        """
        self._obj_meta: Optional[Dict[str, str]] = None
        self._meta_meta: Optional[Dict[str, str]] = None
        self._obj_path: PureS3Path = PureS3Path.from_uri(s3_uri_path)
        self._meta_path: PureS3Path = PureS3Path.from_bucket_key(self._obj_path.bucket + ".meta", self._obj_path.key)
        self._cls: CloudDataType = data_type
        s3_config = s3_config or {}
        self._s3: PickleableS3ClientProxy = PickleableS3ClientProxy(
            aws_access_key_id=s3_config.get("aws_access_key_id"),
            aws_secret_access_key=s3_config.get("aws_secret_access_key"),
            region_name=s3_config.get("region_name"),
            endpoint_url=s3_config.get("endpoint_url"),
            botocore_config_kwargs=s3_config.get("s3_config_kwargs"),
            use_token=s3_config.get("use_token"),
            role_arn=s3_config.get("role_arn"),
            token_duration_seconds=s3_config.get("token_duration_seconds"),
        )
        self._obj_attrs: SimpleNamespace = SimpleNamespace()

        logger.debug(f"{self._obj_path=},{self._meta_path=}")

    @property
    def path(self) -> PureS3Path:
        """
        Get the S3Path of this Cloud Object
        :return: S3Path for this Cloud Object
        """
        return self._obj_path

    @property
    def meta_path(self) -> PureS3Path:
        return self._meta_path

    @property
    def size(self) -> int:
        if not self._obj_meta:
            self.fetch()
        return int(self._obj_meta["ContentLength"])

    @property
    def s3(self) -> S3Client:
        return self._s3

    @property
    def attributes(self) -> Any:
        return self._obj_attrs

    @classmethod
    def from_s3(cls, cloud_object_class, s3_path, s3_config=None, fetch=True) -> "CloudObject":
        co_instance = cls(cloud_object_class, s3_path, s3_config)
        if fetch:
            co_instance.fetch(enforce_obj=True)
        return co_instance

    @classmethod
    def new_from_file(cls, cloud_object_class, file_path, cloud_path, s3_config=None) -> "CloudObject":
        co_instance = cls(cloud_object_class, cloud_path, s3_config)

        if co_instance.exists():
            raise Exception("Object already exists")

        bucket, key = split_s3_path(cloud_path)

        co_instance._s3.upload_file(Filename=file_path, Bucket=bucket, Key=key)
        return co_instance

    def exists(self) -> bool:
        if not self._obj_meta:
            self.fetch()
        return bool(self._obj_meta)

    def is_preprocessed(self) -> bool:
        try:
            head_object(self.s3, bucket=self._meta_path.bucket, key=self._meta_path.key)
            return True
        except KeyError:
            return False

    def fetch(
        self, enforce_obj: bool = False, enforce_meta: bool = False
    ) -> Tuple[Optional[Dict[str, str]], Optional[Dict[str, str]]]:
        """
        Get object metadata from storage with HEAD object request
        :param enforce_obj: True to raise KeyError exception if object key is not found in storage
        :param enforce_meta: True to raise KeyError exception if metadata key is not found in storage
        :return: Tuple for object metadata and objectmeta metadata
        """
        logger.info("Fetching object from S3")
        if not self._obj_meta:
            try:
                res, _ = head_object(self._s3, self._obj_path.bucket, self._obj_path.key)
                self._obj_meta = res
            except KeyError as e:
                self._obj_meta = None
                if enforce_obj:
                    raise e
        if not self._meta_meta:
            try:
                res, encoded_attrs = head_object(self._s3, self._meta_path.bucket, self._meta_path.key)
                self._meta_meta = res
                self._obj_attrs = SimpleNamespace(**load_attributes(encoded_attrs))
            except KeyError as e:
                self._meta_meta = None
                if enforce_meta:
                    raise e
            return self._obj_meta, self._meta_meta

    def preprocess(self, preprocessor_backend: PreprocessorBackendBase, force: bool = False, *args, **kwargs):
        """
        Manually launch the preprocessing job for this cloud object on the specified preprocessing backend
        :param preprocessor_backend: Preprocessor backend instance on to execute the preprocessing job
        :param force: Forces preprocessing on this cloud object, even if it is already preprocessed
        :param args: Optional arguments to pass to the preprocessing job
        :param kwargs:Optional keyword arguments to pass to the preprocessing job
        """
        if not self.is_preprocessed() and not force:
            raise Exception('Object is already preprocessed')

        # FIXME implement this properly
        if issubclass(self._cls.preprocessor, BatchPreprocessor):
            batch_preprocessor: BatchPreprocessor = self._cls.preprocessor(*args, **kwargs)
            preprocessor_backend.preprocess_batch(batch_preprocessor, self)
        elif issubclass(self._cls.preprocessor, MapReducePreprocessor):
            mapreduce_preprocessor: MapReducePreprocessor = self._cls.preprocessor(*args, **kwargs)
            preprocessor_backend.preprocess_map_reduce(mapreduce_preprocessor, self)
        else:
            raise Exception('This object cannot be preprocessed')

    def get_attribute(self, key: str) -> Any:
        """
        Get an attribute of this cloud object. Must be preprocessed first. Raises AttributeError if the
        specified key does not exist.
        :param key: Attribute key
        :return: Attribute
        """
        return getattr(self._obj_attrs, key)

    def partition(self, strategy, *args, **kwargs) -> List[CloudObjectSlice]:
        """
        Apply partitioning strategy on this cloud object
        :param strategy: Partitioning strategy
        :param args: Optional arguments to pass to the partitioning strategy functions
        :param kwargs: Optional key-words arguments to pass to the partitioning strategy
        """
        slices = strategy(self, *args, **kwargs)
        [s._contextualize(self) for s in slices]
        return slices

    @property
    def obj_attrs(self):
        return self._obj_attrs
