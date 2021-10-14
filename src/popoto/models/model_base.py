from src.popoto import ModelField, ModelKey


class ModelException(Exception):
    pass


class RedisModelBase(type):
    """Metaclass for all Redis models."""

    def __new__(mcs, name, bases, attrs, **kwargs):
        print(attrs)

        module = attrs.pop('__module__')
        new_attrs = {'__module__': module}

        for obj_name, obj in attrs.items():
            if obj_name.startswith("__"):
                new_attrs[obj_name] = obj

            elif isinstance(obj, ModelField):
                if obj.value is not None:
                    try:
                        new_attrs[obj_name] = obj.type(obj.value)
                    except ValueError as e:
                        raise ModelException(f"field.value must be of type field.type\n{str(e)}")
                else:
                    new_attrs[obj_name] = obj.type()

                field_meta = ModelField().__dict__
                field_meta.update(obj.__dict__)
                new_attrs[f'{obj_name}_meta'] = field_meta

            elif not obj_name.startswith("_"):
                raise ModelException(
                    f"public model attributes must inherit from class ModelField. "
                    f"Try using a private var (eg. _{obj_name})_"
                )

        new_class = super().__new__(mcs, name, bases, new_attrs)

        return new_class

        # for field in base._meta.private_fields:
        #     new_class.add_to_class(field.name, field)


class RedisModel(metaclass=RedisModelBase):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def save(self):
        """
        todo: validate values
        - field.type
        - field.max_length
        - field.is_sort_key
        - field.is_null
        """
        super().save()
